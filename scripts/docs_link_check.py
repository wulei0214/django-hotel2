# -*- coding: utf-8 -*-
"""docs 文档链接门禁工具（门禁设计 C1）：对照基线 JSON 只拦新增断链，不追存量断链。

用法：check（默认子命令，出现新增断链退出码 1，--baseline <路径> 可覆盖基线路径）；baseline（用当前
扫描结果生成/覆盖基线 JSON，须与修复同一提交，修一条删一条）；report（按目标分组人读输出全部违规，
退出码恒为 0）。口径：扫描全部跟踪 .md（排除 docs/acceptance/）里的 docs/ 前缀提及与省略 docs/ 的
相对提及（共享规范/、PMS1.0_历史归档/、公共资源/、接口文档/、acceptance/、PMS2.0_当前版本/00~07_*、
PMS3.0_后续规划/，按 docs/ 补全尝试解析）；解析成功（命中跟踪文件/目录）、http(s)/mailto/# 开头、
以 / 开头或含上跳段 → 放行；基名命中跟踪文件 → STALE（文件已被移动）；带已知扩展名但目标不存在，
或 docs/ 前缀且无扩展名也未命中目录 → DEAD；基线结构 {"known": {源: [目标, ...]}}。
RE_SHA（十六进制边界保护，防把文件 SHA256 校验和当提交号）与 ROOT（根目录三入口）为门禁家族共用
常量，分别预留给 C5 提交号核验与后续检查项。
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
SUBPREFIX = (
    DOCS_PREFIX, "共享规范/", "PMS1.0_历史归档/", "公共资源/", "接口文档/", "acceptance/",
    "PMS2.0_当前版本/00_项目基线与决策/", "PMS2.0_当前版本/01_产品需求与业务设计/",
    "PMS2.0_当前版本/02_技术架构与数据模型/", "PMS2.0_当前版本/03_API接口与前后端规范/",
    "PMS2.0_当前版本/04_开发实施记录/", "PMS2.0_当前版本/05_测试审计与缺陷整改/",
    "PMS2.0_当前版本/06_验收与发布/", "PMS2.0_当前版本/07_部署运维与数据治理/",
    "PMS3.0_后续规划/",
)
RE_DOCS = re.compile(r"(docs/[^\s`\x60(),;，。；、）]+)")
RE_REL = re.compile(r"((?:共享规范/|PMS1.0_历史归档/|公共资源/|接口文档/|acceptance/"
                    r"|PMS2.0_当前版本/(?:00|01|02|03|04|05|06|07)_[^/\s`]+/|PMS3.0_后续规划/)"
                    r"[^\s`\x60(),;，。；、）]+)")
RE_SHA = re.compile(r"(?<![0-9a-fA-F])[0-9a-f]{40}(?![0-9a-fA-F])")
STRIP_CHARS = "，。；、）…>. "
KNOWN_EXTS = (".md", ".json", ".png", ".sql", ".txt", ".xml", ".log")


def git(args):
    proc = subprocess.run(["git", "-c", "core.quotepath=false"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return proc.stdout.decode("utf-8", "replace")


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


def scan():
    names = tracked()
    files = [p for p in names if p.endswith(".md") and not p.startswith(SCAN_EXCLUDE)]
    dirs = set()
    for p in names:
        segs = p.split("/")[:-1]
        for i in range(len(segs)):
            dirs.add("/".join(segs[:i + 1]) + "/")
    lookups = (set(names), dirs, {os.path.basename(p) for p in names})
    violations = {}
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
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
    return violations, len(files)


def cmd_check(args):
    path = BASELINE
    if "--baseline" in args and args.index("--baseline") + 1 < len(args):
        path = args[args.index("--baseline") + 1]
    if not os.path.exists(path):
        print("基线不存在：%s，先运行 python scripts/docs_link_check.py baseline" % path)
        return 2
    with open(path, encoding="utf-8") as handle:
        known = json.load(handle).get("known", {})
    violations, scanned = scan()
    pairs = [(src, tgt) for tgt, srcs in violations.items() for src in srcs]
    new_pairs = sorted(p for p in pairs if p[1] not in known.get(p[0], []))
    covered = len(pairs) - len(new_pairs)
    print("摘要：扫描文件数=%d 违规目标数=%d 基线覆盖数=%d 新增违规数=%d" % (scanned, len(violations), covered, len(new_pairs)))
    for src, target in new_pairs[:20]:
        print("新增：%s -> %s" % (src, target))
    return 1 if new_pairs else 0


def safe_baseline_path(path):
    """写入前校验：基线路径须为规范相对路径且落在仓库目录内（防路径穿越）。"""
    root = os.path.abspath(os.getcwd())
    full = os.path.abspath(path)
    if os.path.normpath(path) != path or not full.startswith(root + os.sep):
        print("基线路径不安全，拒绝写入：%s" % path)
        sys.exit(2)
    return path


def cmd_baseline(_args):
    violations, scanned = scan()
    pairs = sorted((src, tgt) for tgt, srcs in violations.items() for src in srcs)
    known = {}
    for src, tgt in pairs:
        known.setdefault(src, []).append(tgt)
    payload = json.dumps({"known": known}, ensure_ascii=False, indent=1) + "\n"
    pathlib.Path(safe_baseline_path(BASELINE)).write_text(payload, encoding="utf-8")
    print("基线已写入：%s（扫描文件数=%d，违规目标数=%d）" % (BASELINE, scanned, len(violations)))
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
