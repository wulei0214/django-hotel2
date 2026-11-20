"""仓库敏感凭据扫描器（V12-06 增强版；原 TASK-04 基线）。

设计目标（任务书 V12-06）：
  * 覆盖业务源码、前端源码、配置、Django settings、数据库配置、CI 工作流、
    部署脚本、文档与脚本，而不是只看 docs/ 与 scripts/；
  * 对 `.env`、私钥/证书、二进制、构建产物、依赖目录、Git 历史采用**独立**
    策略，并在 `EXCLUDED_DIRS` 中写明排除理由；
  * 命中即阻断（退出码 1），白名单只能按（文件, 规则）精确豁免且必须写明
    复核理由，禁止整文件豁免掩盖真实密钥。

用法：
    python scripts/scan_secrets.py                # 全仓扫描（CI 口径）
    python scripts/scan_secrets.py apps docs      # 只扫指定路径
    python scripts/scan_secrets.py --json         # 机器可读结果（证据留档）
    python scripts/scan_secrets.py --history      # 额外扫描 Git 历史提交内容

退出码：0 通过；1 命中；2 执行错误（如 Git 不可用时不静默放行）。
"""
import argparse
import json
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 历史口令字面量（已轮换失效，仍禁止再入库——防止"失效口令"被误当有效复用）
KNOWN_REDACTED = [
    "Pms#Demo2026",
    "Accept-2026-Walk",
    "Abc123456",
]

# 排除目录与理由（必须与 docs 中的扫描范围说明保持一致）
EXCLUDED_DIRS = {
    ".git": "版本库对象是二进制包，由 --history 模式按需扫描提交内容",
    ".venv": "第三方依赖，不属于本仓库受控代码",
    "node_modules": "第三方依赖",
    "__pycache__": "字节码产物",
    ".mypy_cache": "工具缓存",
    ".pytest_cache": "工具缓存",
    ".ruff_cache": "工具缓存",
    "staticfiles": "collectstatic 产物，源自 static/",
    "backups": "本机数据库备份，含真实业务数据，禁止入库亦不参与文本扫描",
    ".work": "gitignored 临时工作目录",
    "_wbi_docgov": "一次性文档治理中间产物（交付前应清理）",
    "understand-out": "代码理解工具输出",
    ".idea": "IDE 配置",
    ".ua": "工具输出",
    ".zcode": "工具输出",
    ".deepseek-harness": "工具输出",
    ".mimosa": "工具输出",
    "reports": "本机运行产物（gitignored）",
    "logs": "本机运行日志（gitignored）",
}

TEXT_EXTENSIONS = (
    ".py", ".html", ".htm", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".css", ".scss", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".conf", ".md", ".txt", ".log", ".sh", ".ps1", ".bat", ".cmd",
    ".sql", ".env", ".example", ".sample", ".tf", ".dockerfile",
)
# 无扩展名但属于受控文本的文件
TEXT_FILENAMES = {"Dockerfile", "Makefile", "Procfile", ".env.example"}

# 二进制/私钥类扩展名：不读内容，改为"是否被 Git 跟踪"这一独立策略判定
BINARY_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tar",
    ".whl", ".exe", ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf",
    ".xlsx", ".docx", ".pptx", ".db", ".sqlite3", ".pyc", ".pem", ".key",
    ".p12", ".pfx", ".crt", ".cer",
)

HIGH_SIGNAL_PATTERNS = [
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("private-key-block", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP |DSA )?PRIVATE KEY-----")),
    ("db-uri-with-password", re.compile(
        r"\b(?:mysql|postgres(?:ql)?|redis|mongodb(?:\+srv)?)://[^:\s/]+:[^@\s/]+@")),
    ("bearer-token", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{24,}=*")),
    ("django-secret-key", re.compile(
        r"SECRET_KEY\s*=\s*[\"'](?![\^<$#\{*])[^\"']{32,}[\"']")),
]

# 通用赋值形态：右侧是带引号的疑似口令（排除环境变量/占位符/掩码）。
# 前缀允许下划线：`STRONG_PASSWORD = "..."` / `_TEST_PW_...` 这类常量同样是
# 明文凭据字面量，不能因为标识符带了前缀就漏检。
GENERIC_PATTERN = re.compile(
    r"(?i)(?:^|[_\W])(password|passwd|pwd|secret|token|api_key|apikey|access_key)\b"
    r"\s*[=:]\s*[\"'](?![\^<$#\{*])[A-Za-z0-9#\-\_.!@%]{8,}[\"']"
)

ALLOWED_FILES = {"scan_secrets.py", "secret_scan_allowlist.json"}
DEFAULT_ROOTS = ["."]


def _load_allowlist():
    """白名单条目：{path, rule, reason, reviewed_by, reviewed_at}。

    只按（文件, 规则）精确豁免；`rule` 为 "*" 的整文件豁免一律拒绝加载，
    避免"加一行白名单就把整个文件放过"的掩盖路径。
    """
    path = os.path.join(BASE, "scripts", "secret_scan_allowlist.json")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    entries = []
    for item in data.get("allow", []):
        rule = str(item.get("rule") or "")
        if rule in ("", "*"):
            raise SystemExit("白名单非法：不允许整文件豁免（rule 不能为空或 '*'）。")
        for field in ("path", "reason", "reviewed_by", "reviewed_at"):
            if not str(item.get(field) or "").strip():
                raise SystemExit(f"白名单条目缺少必填字段 {field}：{item}")
        entries.append(item)
    return entries


def _allowlisted(entries, rel, rule):
    for item in entries:
        if item["path"] == rel and item["rule"] == rule:
            return True
    return False


def iter_files(paths):
    for path in paths:
        full = path if os.path.isabs(path) else os.path.join(BASE, path)
        if os.path.isfile(full):
            yield full
            continue
        if not os.path.isdir(full):
            continue
        for root, dirs, names in os.walk(full):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for name in names:
                if name in ALLOWED_FILES:
                    continue
                ext = os.path.splitext(name)[1].lower()
                if ext in BINARY_EXTENSIONS:
                    continue
                if ext in TEXT_EXTENSIONS or name in TEXT_FILENAMES:
                    yield os.path.join(root, name)


def _git_ls_files():
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=BASE, capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Git 不可用，无法校验 .env/私钥是否被跟踪：{exc}")
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def _scan_tracked_sensitive_files(tracked):
    """独立策略：敏感文件绝不入库，判定依据是 Git 索引而非文件内容。"""
    hits = []
    for name in (".env", "scripts/r6_env.txt"):
        if name in tracked:
            hits.append((name, "tracked-sensitive-file",
                         "该文件必须 gitignore，不得进入版本库"))
    for path in tracked:
        ext = os.path.splitext(path)[1].lower()
        if ext in (".pem", ".key", ".p12", ".pfx"):
            hits.append((path, "tracked-private-key", "私钥/证书不得入库"))
    return hits


def _display_path(full):
    """仓库内文件显示相对路径；仓库外（如 CI 自检用的临时目录）显示绝对路径。

    Windows 下跨盘符时 `os.path.relpath` 会直接抛 ValueError，取证脚本不能
    因此崩掉——它要能对"仓库外的注入样例"同样给出命中结论。
    """
    try:
        return os.path.relpath(full, BASE).replace(os.sep, "/")
    except ValueError:
        return full.replace(os.sep, "/")


def scan(paths, *, allowlist, tracked):
    hits = []
    for full in iter_files(paths):
        rel = _display_path(full)
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for literal in KNOWN_REDACTED:
            if literal in text and not _allowlisted(
                allowlist, rel, "known-redacted-literal"
            ):
                hits.append((rel, "known-redacted-literal", literal))
        for rule, pattern in HIGH_SIGNAL_PATTERNS:
            for match in pattern.finditer(text):
                if not _allowlisted(allowlist, rel, rule):
                    hits.append((rel, rule, match.group(0)[:60]))
        # 通用赋值只在活的配置/源码/文档中判定：证据日志（.log/.txt）里
        # password= 多为强度校验负样例，全量匹配会产生成片噪声并淹没真命中。
        if not full.endswith((".log", ".txt")):
            for match in GENERIC_PATTERN.finditer(text):
                if not _allowlisted(allowlist, rel, "generic-assignment"):
                    hits.append((rel, "generic-assignment", match.group(0)[:60]))
    hits.extend(_scan_tracked_sensitive_files(tracked))
    return hits


def scan_history():
    """扫描 Git 历史提交内容中的高信号密钥（按需执行，耗时不进默认 CI）。"""
    try:
        out = subprocess.run(
            ["git", "log", "-p", "--all", "--no-color"],
            cwd=BASE, capture_output=True, text=True, check=True,
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Git 历史扫描失败：{exc}")
    hits = []
    for rule, pattern in HIGH_SIGNAL_PATTERNS:
        for match in pattern.finditer(out.stdout):
            hits.append(("<git-history>", rule, match.group(0)[:60]))
    for literal in KNOWN_REDACTED:
        if literal in out.stdout:
            hits.append(("<git-history>", "known-redacted-literal", literal))
    return hits


def main(argv=None):
    parser = argparse.ArgumentParser(description="仓库敏感凭据扫描")
    parser.add_argument("paths", nargs="*", default=None, help="扫描路径（默认全仓）")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="输出 JSON（机器可读证据）")
    parser.add_argument("--history", action="store_true",
                        help="额外扫描 Git 历史提交内容")
    args = parser.parse_args(argv)

    allowlist = _load_allowlist()
    tracked = _git_ls_files()
    hits = scan(args.paths or DEFAULT_ROOTS, allowlist=allowlist, tracked=tracked)
    if args.history:
        hits.extend(scan_history())

    if args.as_json:
        print(json.dumps(
            {
                "excluded_dirs": EXCLUDED_DIRS,
                "allowlist_size": len(allowlist),
                "hit_count": len(hits),
                "hits": [
                    {"path": path, "rule": rule, "snippet": snippet}
                    for path, rule, snippet in hits
                ],
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        for rel, kind, snippet in hits:
            print(f"HIT[{kind}] {rel}: {snippet}")
        if hits:
            print(f"扫描阻断：{len(hits)} 处疑似凭据，禁止提交。")
        else:
            print("扫描通过：未发现凭据字面量。")

    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
