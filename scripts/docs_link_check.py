# -*- coding: utf-8 -*-
"""docs 文档门禁工具（门禁设计 C1/C2/C4/C5/C6/C7）：对照基线 JSON 只拦新增违规，不追存量。

用法：check（默认子命令，C1/C2/C4/C5/C6 任一新类违规退出码 1，C7 仅打印警告不计退出码；
--baseline <路径> 可覆盖基线路径；--against <ref> 默认 origin/main，C2/C6 用其与 HEAD 的
merge-base 分叉点对比工作区（含暂存），避免把本地领先的合法提交算进本轮新增/受保护变更）；
baseline（用当前扫描结果生成/覆盖基线 JSON，须与修复同一提交，修一条删一条）；report（按
目标分组人读输出 C1 全部违规，退出码恒为 0）。
C1 断链：扫描全部跟踪 .md（排除 docs/acceptance/）里的 docs/ 前缀提及与省略 docs/ 的相对提及
（共享规范/、PMS1.0_历史归档/、公共资源/、接口文档/、acceptance/、PMS2.0_当前版本/00~07_*、
PMS3.0_后续规划/，按 docs/ 补全尝试解析）；解析成功（命中跟踪文件/目录）、http(s)/mailto/# 开头、
以 / 开头或含上跳段 → 放行；基名命中跟踪文件 → STALE（文件已被移动）；带已知扩展名但目标不存在，
或 docs/ 前缀且无扩展名也未命中目录 → DEAD。
C4 内容重复：`git ls-tree -r HEAD` 按 blob SHA 分组，组内文件数 ≥2 且至少一个路径落在扫描范围
（docs/ 非 acceptance、根目录 .md、static/images/）→ 违规，输出 `C4重复: <p1> == <p2>`；受控镜像
openapi.json 路径对在 C4_MIRROR 白名单放行；基线键 c4_known（组路径列表的列表）。
C5 SHA 可解析性：对 C1 同一批 md 文本用 RE_SHA 提取 40 位十六进制，`git cat-file --batch-check`
批量验证，missing 行 → 违规；基线键 c5_known（{源: [sha, ...]}）。
C6 证据保护：diff 分叉点..工作区（含暂存）的三个受保护目录 PROTECTED_DIRS，状态以 A 开头（新增）
放行，M/D/R*/其余均拦 → 违规；基线键 c6_known（{路径: 状态}，改名列记 "旧 -> 新"）。
C2 新增文档元数据：diff 分叉点..工作区（含暂存）--diff-filter=A 新增的 md（pathspec docs/ 与
*.md），存量文件一律豁免；再豁免 docs/acceptance/ 与 历史审计/ 两类证据目录（C2_EXEMPT）；
前 6 行内无以 `> 文档编号：` 开头的行 → 违规；基线键 c2_known（[文件, ...]）。
C7 活文档追加（warn 级）：固定清单 LIVING_DOCS 各活文档统计 `^## ` 标题，同一标题文本出现
≥2 次 → 警告输出；每次现算，不入基线、不计退出码。
基线结构 {"known": {源: [目标, ...]}, "c2_known": [文件, ...], "c4_known": [[路径, ...], ...],
"c5_known": {源: [sha, ...]}, "c6_known": {路径: 状态}}；RE_SHA（十六进制边界保护，防把文件
SHA256 校验和当提交号）与 ROOT（根目录三入口）为门禁家族共用常量，ROOT 预留给后续检查项。
"""
import json
import os
import pathlib
import re
import subprocess
import sys

BASELINE = "docs/共享规范/docs_gate_baseline.json"
SCAN_EXCLUDE = "docs/acceptance/"
DOCS_PREFIX = "docs/"
ROOT = ("README.md", "ARCHITECTURE.md", "AGENTS.md")
DEFAULT_AGAINST = "origin/main"
PROTECTED_DIRS = (
    "docs/acceptance",
    "docs/PMS2.0_当前版本/05_测试审计与缺陷整改/历史审计",
    "docs/PMS2.0_当前版本/06_验收与发布/evidence",
)
C4_MIRROR = [
    "docs/PMS2.0_当前版本/03_API接口与前后端规范/接口文档/openapi.json",
    "docs/接口文档/openapi.json",
]
C4_IMAGE_DIR = "static/images/"
C2_EXEMPT = (SCAN_EXCLUDE, "docs/PMS2.0_当前版本/05_测试审计与缺陷整改/历史审计/")
METADATA_MARK = "> 文档编号："
METADATA_HEAD_LINES = 6
LIVING_DOCS = (
    "docs/共享规范/PMS当前状态总览.md",
    "docs/共享规范/文档治理阶段二_文档权威关系矩阵_2026-11-14.md",
    "docs/共享规范/文档治理阶段二_新开发人员阅读路线_2026-11-14.md",
    "docs/文档索引.md",
)
SUBPREFIX = (
    DOCS_PREFIX, "共享规范/", "PMS1.0_历史归档/", "公共资源/", "接口文档/", "acceptance/",
    "PMS2.0_当前版本/00_项目基线与决策/", "PMS2.0_当前版本/01_产品需求与业务设计/",
    "PMS2.0_当前版本/02_技术架构与数据模型/", "PMS2.0_当前版本/03_API接口与前后端规范/",
    "PMS2.0_当前版本/04_开发实施记录/", "PMS2.0_当前版本/05_测试审计与缺陷整改/",
    "PMS2.0_当前版本/06_验收与发布/", "PMS2.0_当前版本/07_部署运维与数据治理/",
    "PMS3.0_后续规划/",
)
RE_DOCS = re.compile(r"(docs/[^\s`\x60(),;，。；、）》§*{}]+)")
RE_REL = re.compile(r"((?:共享规范/|PMS1.0_历史归档/|公共资源/|接口文档/|acceptance/"
                    r"|PMS2.0_当前版本/(?:00|01|02|03|04|05|06|07)_[^/\s`]+/|PMS3.0_后续规划/)"
                    r"[^\s`\x60(),;，。；、）]+)")
RE_SHA = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{40}(?![0-9a-fA-F])")
STRIP_CHARS = "，。；、）…>. "
KNOWN_EXTS = (".md", ".json", ".png", ".sql", ".txt", ".xml", ".log")


def git(args):
    proc = subprocess.run(["git", "-c", "core.quotepath=false"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return proc.stdout.decode("utf-8", "replace")


def _opt(args, flag, default):
    if flag in args and args.index(flag) + 1 < len(args):
        return args[args.index(flag) + 1]
    return default


def tracked():
    return [line for line in git(["ls-files"]).splitlines() if line]


def classify(mention, lookups):
    t = mention.strip(STRIP_CHARS).split("#", 1)[0].strip(STRIP_CHARS)
    files, dirs, bases = lookups
    if (not t or t.startswith(("http://", "https://", "mailto:", "#")) or t.startswith("/")
            or os.path.normpath(t).replace(os.sep, "/") != t):
        return None
    cands = [t]
    for prefix in SUBPREFIX:
        if t.startswith(prefix):
            cands.append(DOCS_PREFIX + t)
    if any(c in files or c in dirs or c + "/" in dirs for c in cands):
        return None
    if os.path.basename(t) in bases:
        return "STALE"
    ext = os.path.splitext(t)[1].lower()
    if ext in KNOWN_EXTS or (t.startswith(DOCS_PREFIX) and not ext):
        return "DEAD"
    return None


def collect():
    """C1 扫描并缓存同一批 md 文本（C5 复用），返回（违规 {目标: {源, ...}}, 文件列表, 文本字典）。"""
    names = tracked()
    files = [p for p in names if p.endswith(".md") and not p.startswith(SCAN_EXCLUDE)]
    dirs = set()
    for p in names:
        segs = p.split("/")[:-1]
        for i in range(len(segs)):
            dirs.add("/".join(segs[:i + 1]) + "/")
    lookups = (set(names), dirs, {os.path.basename(p) for p in names})
    violations = {}
    texts = {}
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        texts[path] = text
        found = set()
        hits = [(m.group(0), m.start()) for m in RE_DOCS.finditer(text)]
        hits += [(m.group(1), m.start()) for m in RE_REL.finditer(text)
                 if m.start() and text[m.start() - 1] != "/"]
        for raw, _pos in hits:
            target = raw.strip(STRIP_CHARS)
            if target and classify(target, lookups):
                found.add(target)
        for target in found:
            violations.setdefault(target, set()).add(path)
    return violations, files, texts


def scan():
    violations, files, _texts = collect()
    return violations, len(files)


def _c4_in_scope(path):
    if path.startswith(SCAN_EXCLUDE):
        return False
    if path.startswith(DOCS_PREFIX) or path.startswith(C4_IMAGE_DIR):
        return True
    return "/" not in path and path.endswith(".md")


def scan_duplicates():
    out = git(["ls-tree", "-r", "HEAD"])
    groups = {}
    for line in out.splitlines():
        meta, sep, path = line.partition("\t")
        if not sep:
            continue
        parts = meta.split()
        if len(parts) < 3 or parts[1] != "blob":
            continue
        groups.setdefault(parts[2], []).append(path)
    violations = []
    for paths in groups.values():
        if len(paths) < 2 or not any(_c4_in_scope(p) for p in paths):
            continue
        ordered = sorted(paths)
        if ordered == C4_MIRROR:
            continue
        violations.append(ordered)
    return sorted(violations)


def scan_shas(texts):
    """对 C1 同一批 md 文本提取 40 位十六进制并批量核验，返回（{源: [sha, ...]}, 不可解析 SHA 数）。"""
    found = {}
    for path, text in texts.items():
        for m in RE_SHA.finditer(text):
            found.setdefault(m.group(0), set()).add(path)
    if not found:
        return {}, 0
    query = ("\n".join(sorted(found)) + "\n").encode("ascii")
    proc = subprocess.run(["git", "cat-file", "--batch-check"], input=query, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    missing = set()
    for line in proc.stdout.decode("ascii", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "missing":
            missing.add(parts[0])
    violations = {}
    for sha in sorted(missing):
        for src in sorted(found[sha]):
            violations.setdefault(src, []).append(sha)
    return violations, len(missing)


def scan_protected(against):
    """diff 分叉点（against 与 HEAD 的 merge-base）..工作区（含暂存）的受保护目录，返回 [(路径, 状态)]。"""
    fork = git(["merge-base", against, "HEAD"]).strip()
    out = git(["diff", "--name-status", fork, "--"] + list(PROTECTED_DIRS))
    records = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, shown = parts[0], parts[1]
        if len(parts) > 2:
            shown += " -> " + parts[2]
        records.append((shown, status))
    return records


def _has_doc_id(path):
    """前 METADATA_HEAD_LINES 行内是否存在以 METADATA_MARK 开头的行。"""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as handle:
            head = handle.read().splitlines()
    except OSError:
        return False
    return any(line.startswith(METADATA_MARK) for line in head[:METADATA_HEAD_LINES])


def scan_metadata(against):
    """C2：diff 分叉点..工作区（含暂存）--diff-filter=A 新增的 md，返回缺编号元数据块的文件列表。"""
    fork = git(["merge-base", against, "HEAD"]).strip()
    out = git(["diff", "--name-only", "--diff-filter=A", fork, "--", "docs/", "*.md"])
    missing = []
    for line in out.splitlines():
        path = line.strip()
        if not path.endswith(".md") or path.startswith(C2_EXEMPT):
            continue
        if not _has_doc_id(path):
            missing.append(path)
    return sorted(missing)


def scan_living():
    """C7（warn 级，不入基线不计退出码）：各活文档 `^## ` 同一标题文本出现 ≥2 次的 [(文件, 标题)]。"""
    repeats = []
    for path in LIVING_DOCS:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig", errors="replace") as handle:
            text = handle.read()
        counts = {}
        for line in text.splitlines():
            if line.startswith("## "):
                title = line[3:].strip()
                counts[title] = counts.get(title, 0) + 1
        repeats.extend((path, title) for title, count in counts.items() if count >= 2)
    return repeats


def cmd_check(args):
    path = _opt(args, "--baseline", BASELINE)
    against = _opt(args, "--against", DEFAULT_AGAINST)
    if not os.path.exists(path):
        print("基线不存在：%s，先运行 python scripts/docs_link_check.py baseline" % path)
        return 2
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    known = payload.get("known", {})
    c2_known = payload.get("c2_known", [])
    c4_known = [sorted(g) for g in payload.get("c4_known", [])]
    c5_known = payload.get("c5_known", {})
    c6_known = payload.get("c6_known", {})
    link_violations, files, texts = collect()
    pairs = [(src, tgt) for tgt, srcs in link_violations.items() for src in srcs]
    new_pairs = sorted(p for p in pairs if p[1] not in known.get(p[0], []))
    dup_groups = scan_duplicates()
    new_dups = [g for g in dup_groups if g not in c4_known]
    sha_violations, sha_missing = scan_shas(texts)
    new_shas = {src: [s for s in shas if s not in c5_known.get(src, [])] for src, shas in sha_violations.items()}
    try:
        prot = scan_protected(against)
        meta_missing = scan_metadata(against)
    except subprocess.CalledProcessError:
        print("C2/C6无法执行：--against %s 与 HEAD 无 merge-base" % against)
        return 2
    new_prot = [(shown, status) for shown, status in prot if c6_known.get(shown) != status]
    new_meta = [f for f in meta_missing if f not in c2_known]
    repeats = scan_living()
    new_total = (len(new_pairs) + len(new_meta) + len(new_dups)
                 + sum(len(v) for v in new_shas.values()) + len(new_prot))
    print("摘要：扫描文件数=%d 断链对=%d 重复组=%d 不可解析SHA=%d 受保护变更=%d 新增缺元数据=%d 重复标题=%d 新增违规数=%d"
          % (len(files), len(pairs), len(dup_groups), sha_missing, len(prot), len(meta_missing), len(repeats), new_total))
    for src, target in new_pairs[:20]:
        print("新增：%s -> %s" % (src, target))
    for target in new_meta[:20]:
        print("C2元数据: %s 缺少编号元数据块" % target)
    for group in new_dups[:20]:
        print("C4重复: " + " == ".join(group))
    for src in sorted(new_shas):
        for sha in new_shas[src][:20]:
            print("C5SHA：%s -> %s" % (src, sha))
    for shown, status in new_prot[:20]:
        print("C6保护：%s %s" % (status, shown))
    for target, title in repeats[:20]:
        print("C7追加(警告): %s 重复标题 %s" % (target, title))
    return 1 if new_total else 0


def safe_baseline_path(path):
    """写入前校验：基线路径须为规范相对路径且落在仓库目录内（防路径穿越）。"""
    root = os.path.abspath(os.getcwd())
    full = os.path.abspath(path)
    if os.path.normpath(path).replace(os.sep, "/") != path or not full.startswith(root + os.sep):
        print("基线路径不安全，拒绝写入：%s" % path)
        sys.exit(2)
    return path


def cmd_baseline(args):
    against = _opt(args, "--against", DEFAULT_AGAINST)
    violations, files, texts = collect()
    pairs = sorted((src, tgt) for tgt, srcs in violations.items() for src in srcs)
    known = {}
    for src, tgt in pairs:
        known.setdefault(src, []).append(tgt)
    c4_known = [list(g) for g in scan_duplicates()]
    c5_known, sha_missing = scan_shas(texts)
    c6_known = dict(scan_protected(against))
    c2_known = scan_metadata(against)
    payload = json.dumps({"known": known, "c2_known": c2_known, "c4_known": c4_known, "c5_known": c5_known, "c6_known": c6_known}, ensure_ascii=False, indent=1) + "\n"
    pathlib.Path(safe_baseline_path(BASELINE)).write_text(payload, encoding="utf-8")
    print("基线已写入：%s（扫描文件数=%d，断链对=%d，重复组=%d，不可解析SHA=%d，受保护目录变更=%d，新增缺元数据=%d）"
          % (BASELINE, len(files), len(pairs), len(c4_known), sha_missing, len(c6_known), len(c2_known)))
    return 0


def cmd_report(_args):
    violations, scanned = scan()
    print("扫描文件数=%d 违规目标数=%d" % (scanned, len(violations)))
    for target in sorted(violations):
        for src in sorted(violations[target]):
            print("%s <- %s" % (target, src))
    return 0


def main(argv):
    handlers = {"check": cmd_check, "baseline": cmd_baseline, "report": cmd_report}
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd not in handlers:
        print("用法：python scripts/docs_link_check.py [check|baseline|report]")
        return 2
    return handlers[cmd](argv[2:])


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv))
