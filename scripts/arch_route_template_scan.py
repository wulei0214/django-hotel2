# -*- coding: utf-8 -*-
"""ARCH-02 / ARCH-03 旧路由与旧模板引用扫描（交付物 DEL-06 / DEL-07 的证据生成器）。

任务依据：《PMS 2.0 剩余事项裁定执行、G14 最终验收与 PMS 3.0 启动准备任务书》
PMS2-FINAL-DECISION-EXEC-002 §3.1（ARCH-02 旧路由退役）/ §3.2（ARCH-03 旧模板处置）。

All inputs are read from git blobs at <sha>, never from the working tree, so a
dirty tree (other sessions' uncommitted files) cannot leak into the result.

usage: python scripts/arch_route_template_scan.py [<sha>] [--out <dir>]
outputs (in <out>, default .work/arch-scan):
  arch02_routes.json     one record per legacy page route declared in hotel/urls.py
  arch03_templates.json  one record per template under templates/
  arch02_03_summary.txt  headline counts

Dispositions printed here are *suggestions* derived by fixed rules; the final
KEEP / REDIRECT / 410 / MIGRATE / DELETE verdict for each row is made when the
matrix document is written, with this file as its evidence.
"""
import argparse
import ast
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_parser = argparse.ArgumentParser(description="ARCH-02/03 旧路由与旧模板引用扫描")
_parser.add_argument("sha", nargs="?", default="HEAD", help="绑定 SHA（默认 HEAD）")
_parser.add_argument("--out", default=str(REPO_ROOT / ".work" / "arch-scan"),
                     help="输出目录（默认 .work/arch-scan）")
ARGS = _parser.parse_args()

SHA = ARGS.sha
OUT = Path(ARGS.out)
OUT.mkdir(parents=True, exist_ok=True)
SCAN_PREFIXES = ("templates/", "static/", "hotelapp/", "apps/", "hotel/", "scripts/")
TEXT_EXT = (".py", ".html", ".js", ".css", ".json", ".txt")
TEST_RE = re.compile(r"(^|/)(test_[^/]*\.py|tests\.py)$")

WRITE_ATTRS = {
    "save", "create", "update", "delete", "bulk_create", "bulk_update",
    "get_or_create", "update_or_create",
}
SERVICE_TOKENS = (
    "approve", "reject", "execute", "post_entry", "reverse", "collect", "refund",
    "checkin", "checkout", "cancel", "confirm", "enqueue", "dispatch",
    "set_password", "revoke", "grant", "assign", "record",
)


def git(*args, data=None):
    p = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args], input=data, capture_output=True
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace"))
    return p.stdout


def blob(path):
    return git("show", f"{SHA}:{path}").decode("utf-8")


def ls_files(prefix):
    out = git("ls-tree", "-r", "-z", "--name-only", SHA, "--", prefix)
    return [x for x in out.decode("utf-8").split("\0") if x]


def load_blobs(paths):
    """git cat-file --batch -> {path: text}"""
    req = "".join(f"{SHA}:{p}\n" for p in paths).encode("utf-8")
    raw = git("cat-file", "--batch", data=req)
    res, pos = {}, 0
    for p in paths:
        nl = raw.index(b"\n", pos)
        size = int(raw[pos:nl].split()[2])
        res[p] = raw[nl + 1 : nl + 1 + size].decode("utf-8", "replace")
        pos = nl + 1 + size + 1
    return res


def dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def bound_regex(target):
    """Literal match not glued to a longer path / identifier."""
    return re.compile(
        r"(?<![A-Za-z0-9_/])" + re.escape(target) + r"(?![A-Za-z0-9_])"
    )


# ---------------------------------------------------------------- file corpus
files = []
for prefix in SCAN_PREFIXES:
    files += [p for p in ls_files(prefix) if p.lower().endswith(TEXT_EXT)]
corpus = load_blobs(files)
tests = {p: t for p, t in corpus.items() if TEST_RE.search(p) and p.endswith(".py")}
code = {
    p: t
    for p, t in corpus.items()
    if p.endswith(".py") and p not in tests and p != "hotel/urls.py"
}
tpl_files = {p: t for p, t in corpus.items() if p.startswith("templates/")}
static = {p: t for p, t in corpus.items() if p.startswith("static/")}

# ---------------------------------------------------------------- ARCH-02 routes
urls_src = blob("hotel/urls.py")
utree = ast.parse(urls_src)
routes = []
for node in ast.walk(utree):
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("path", "re_path")
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        continue
    a1 = node.args[1] if len(node.args) > 1 else None
    name = dotted(a1) if a1 is not None else None
    if not name and a1 is not None:
        name = ast.get_source_segment(urls_src, a1)
    routes.append(
        {"line": node.args[0].lineno, "url": node.args[0].value, "view": name}
    )
routes.sort(key=lambda r: r["line"])

views_src = blob("hotelapp/views.py")
vtree = ast.parse(views_src)
funcs = {
    n.name: n
    for n in vtree.body
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
}


def analyse_view(fn):
    node = funcs[fn]
    rec = {
        "line": node.lineno,
        "decorators": [ast.get_source_segment(views_src, d) for d in node.decorator_list],
        "docstring": (ast.get_docstring(node) or "").strip().split("\n")[0][:120],
        "templates": [],
        "status": [],
        "literal_410": False,
        "write_hits": [],
        "service_calls": [],
        "orm_used": False,
        "redirect": False,
        "json": False,
    }
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant):
            if isinstance(sub.value, str) and sub.value.endswith(".html"):
                rec["templates"].append(sub.value)
            elif sub.value == 410:
                rec["literal_410"] = True
        elif isinstance(sub, ast.Attribute) and sub.attr == "objects":
            rec["orm_used"] = True
        elif isinstance(sub, ast.Call):
            name = dotted(sub.func)
            last = name.rsplit(".", 1)[-1]
            for kw in sub.keywords:
                if (
                    kw.arg == "status"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, int)
                ):
                    rec["status"].append(kw.value.value)
            if isinstance(sub.func, ast.Attribute) and sub.func.attr in WRITE_ATTRS:
                rec["write_hits"].append(name)
            if last == "redirect" or name in (
                "HttpResponseRedirect",
                "HttpResponsePermanentRedirect",
            ):
                rec["redirect"] = True
            if last == "JsonResponse":
                rec["json"] = True
            if any(tok in last.lower() for tok in SERVICE_TOKENS):
                rec["service_calls"].append(name)
    for key in ("templates", "write_hits", "service_calls"):
        rec[key] = sorted(set(rec[key]))
    rec["status"] = sorted(set(rec["status"]))
    return rec


def refs(target, space):
    rx = bound_regex(target)
    hits = []
    for path, text in space.items():
        for no, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{path}:{no}")
    return hits


COMPAT = {"home/", "login/", "logout/", "register/"}
ARCH05 = {"force-password-change/", "system/"}
rows = []
for r in routes:
    if not (r["view"] or "").startswith("views."):
        continue
    fn = r["view"].split(".", 1)[1]
    rec = analyse_view(fn) if fn in funcs else None
    url = r["url"]
    literal = url.split("<")[0]
    if rec is None:
        suggestion = "MISSING"
    elif url in ARCH05 and rec["write_hits"]:
        suggestion = "KEEP-WRITE"  # ARCH-05 auxiliary page that does write (self password change)
    elif url in COMPAT or url in ARCH05:
        suggestion = "KEEP"
    elif rec["literal_410"] and not rec["write_hits"]:
        suggestion = "410"
    elif rec["write_hits"]:
        suggestion = "WRITE?"
    elif rec["redirect"] and not rec["templates"]:
        suggestion = "REDIRECT?"
    else:
        suggestion = "READ?"
    rows.append(
        {
            "line": r["line"],
            "url": url,
            "view": r["view"],
            "suggest": suggestion,
            "view": r["view"],
            "view_detail": rec,
            "refs": {
                "templates": refs(literal, tpl_files),
                "static": refs(literal, static),
                "code": refs(literal, code),
                "tests": refs(literal, tests),
            },
        }
    )

# ---------------------------------------------------------------- ARCH-03 templates
EXT_RE = re.compile(r"""{%\s*(?:extends|include)\s+["']([^"']+)["']""")
templates = []
for path in sorted(tpl_files):
    rel = path[len("templates/") :]
    target = rel if "/" in rel else rel.split("/")[-1]
    included_by = sorted(
        other[len("templates/") :]
        for other, text in tpl_files.items()
        if other != path and any(m == rel or m == target for m in EXT_RE.findall(text))
    )
    rendered_by = sorted(
        row["url"] for row in rows if target in (row["view_detail"] or {}).get("templates", [])
    )
    code_refs = refs(target, code)
    test_refs = refs(target, tests)
    static_refs = refs(target, static)
    if rel.startswith("pms/"):
        suggestion = "N/A-PMS2"
    elif rel.split("/")[0] in ("admin", "identity", "platform", "public"):
        suggestion = "KEEP-PMS2-PAGE"
    elif included_by and (code_refs or rendered_by):
        suggestion = "KEEP"
    elif included_by:
        suggestion = "KEEP-PARTIAL"
    elif rendered_by and all(row["suggest"] == "410" for row in rows if target in (row["view_detail"] or {}).get("templates", [])):
        suggestion = "RETIRE-WITH-ROUTE?"
    elif rendered_by or code_refs or static_refs:
        suggestion = "IN-USE"
    elif test_refs:
        suggestion = "TEST-ONLY?"
    else:
        suggestion = "DELETE?"
    templates.append(
        {
            "template": rel,
            "bytes": len(tpl_files[path].encode("utf-8")),
            "suggest": suggestion,
            "included_by": included_by,
            "rendered_by_routes": rendered_by,
            "refs": {
                "code": code_refs,
                "static": static_refs,
                "tests": test_refs,
            },
        }
    )

# ---------------------------------------------------------------- outputs
(OUT / "arch02_routes.json").write_text(
    json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
)
(OUT / "arch03_templates.json").write_text(
    json.dumps(templates, ensure_ascii=False, indent=1), encoding="utf-8"
)
bound = git("rev-parse", SHA).decode().strip()
rc = Counter(r["suggest"] for r in rows)
tc = Counter(t["suggest"] for t in templates)
lines = [
    f"bound_sha={bound}",
    f"corpus_files={len(corpus)} (tests={len(tests)}, code={len(code)}, templates={len(tpl_files)}, static={len(static)})",
    f"ARCH-02 legacy page routes (views.*)={len(rows)}  by_suggestion={dict(sorted(rc.items()))}",
    f"ARCH-03 templates total={len(templates)}  by_suggestion={dict(sorted(tc.items()))}",
]
(OUT / "arch02_03_summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
print("\n".join(lines))
print("--- routes not suggested 410 ---")
for r in rows:
    d = r["view_detail"] or {}
    print(
        "{0:<9} {1:<36} {2:<28} tpl={3} writes={4} svc={5}".format(
            r["suggest"],
            r["url"],
            r["view"],
            ",".join(d.get("templates", [])) or "-",
            len(d.get("write_hits", [])),
            len(d.get("service_calls", [])),
        )
    )
print("--- templates ---")
for t in templates:
    print(
        "{0:<16} {1:<34} inc_by={2} routes={3} code={4} tests={5}".format(
            t["suggest"],
            t["template"],
            len(t["included_by"]),
            len(t["rendered_by_routes"]),
            len(t["refs"]["code"]),
            len(t["refs"]["tests"]),
        )
    )
