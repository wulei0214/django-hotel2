"""文档与提交内容敏感凭据扫描器（TASK-04 整改工具）。

扫描仓库内文档/代码中的测试账号口令字面量与常见密钥形态。命中即退出码 1。
设计基线：口令只允许存在于 gitignore 的 scripts/r6_env.txt 与环境变量。
用法：python scripts/scan_secrets.py [路径...]
默认扫描 docs/ 与 scripts/（排除 r6_env.txt 与 .venv）。
"""
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 历史口令字面量（已轮换失效，仍禁止再入文档——防止"失效口令"被误当作有效复用）
KNOWN_REDACTED = [
    "Pms#Demo2026",
    "Accept-2026-Walk",
    "Abc123456",
]
# 通用密钥形态：赋值右边是长随机串（排除变量名本身、占位符与文档化正则示例）
GENERIC_PATTERNS = [
    re.compile(r"(password|passwd|pwd|secret|token)\s*[=:]\s*[\"'](?![\^<$#\{])[A-Za-z0-9#\-\_.]{8,}[\"']",
               re.IGNORECASE),
]
ALLOWED_FILES = {"r6_env.txt", "scan_secrets.py"}
ALLOWED_DIRS = (".venv", "node_modules", "__pycache__", ".git")


def iter_files(paths):
    for path in paths:
        full = os.path.join(BASE, path) if not os.path.isabs(path) else path
        if os.path.isfile(full):
            yield full
        elif os.path.isdir(full):
            for root, dirs, names in os.walk(full):
                dirs[:] = [d for d in dirs if d not in ALLOWED_DIRS]
                for name in names:
                    if name in ALLOWED_FILES:
                        continue
                    if name.endswith((".md", ".py", ".html", ".txt", ".log", ".json", ".yml", ".yaml")):
                        yield os.path.join(root, name)


def main(argv):
    paths = argv[1:] or ["docs", "scripts"]
    hits = []
    for full in iter_files(paths):
        rel = os.path.relpath(full, BASE)
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for literal in KNOWN_REDACTED:
            if literal in text:
                hits.append((rel, "known-redacted-literal", literal))
        for pat in GENERIC_PATTERNS:
            # .log/.txt 为历史证据留档（append-only），其中 password= 多为
            # 测试夹具负样例（如密码强度校验的弱口令断言）；密钥扫描聚焦
            # 活文档（md/py/html/json/yml）。
            if full.endswith((".log", ".txt")):
                continue
            for m in pat.finditer(text):
                hits.append((rel, "generic-assignment", m.group(0)[:60]))
    if hits:
        for rel, kind, snippet in hits:
            print(f"HIT[{kind}] {rel}: {snippet}")
        print(f"扫描阻断：{len(hits)} 处疑似凭据，禁止提交。")
        return 1
    print("扫描通过：未发现凭据字面量。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
