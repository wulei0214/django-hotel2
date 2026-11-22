"""Walk-in 旧入口（410）访问统计（V12-09 / V13-03）。

用途：在部署侧访问日志上统计旧入口 `POST /api/v1/front-desk/walk-in` 的
真实命中情况，用于"观察 → 迁移 → 410 → 退役"的收口判定。开发/本地证据目录
没有生产日志，因此本工具只负责"能算准"，样本由部署侧提供。

支持两种输入：
  * 常见 Web 服务器 combined/common 访问日志（nginx/Apache/IIS 导出均可）；
  * 单行 JSON（如 `{"request": "POST /api/v1/front-desk/walk-in", "status": 410}`）。

V13-03 方法口径（任务书 §3）：
  * 默认只统计旧入口的 `POST` 命中（`--method POST`，可改 `ALL` 或其他方法）；
  * 旧入口其他 HTTP 方法**分开统计**（`legacy_by_method` / `legacy_other_hits`），
    不与 POST 口径混计；
  * 旧入口与标准入口 `POST /api/v1/front-desk/walk-ins` 始终分开计数；
  * 状态码、按日、UA 族、来源去重等聚合只作用于当前方法口径；
  * 隐私约束不变：输出只含聚合计数与 User-Agent 族，**不输出客户端 IP**（只输出
    不可逆短哈希用于区分来源数量），不输出 cookie / Authorization / 请求体。

用法：
    python scripts/walkin_410_stats.py access.log
    python scripts/walkin_410_stats.py access.log --json
    python scripts/walkin_410_stats.py logs/ --pattern "access*.log" --date 20/Nov/2026
    python scripts/walkin_410_stats.py access.log --method ALL   # 全方法口径

退出码：0 统计完成；2 无可解析样本（不静默当作"零残留调用"）。
"""

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict

LEGACY_PATH = "/api/v1/front-desk/walk-in"
STANDARD_PATH = "/api/v1/front-desk/walk-ins"

# combined: host - - [time] "METHOD path HTTP/x" status bytes "referer" "ua"
COMBINED = re.compile(
    r"^(?P<host>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] "
    r'"(?P<method>[A-Z]+) (?P<path>[^ ]+)[^"]*" (?P<status>\d{3}) (?P<bytes>\S+)'
    r'(?: "(?P<referer>[^"]*)" "(?P<ua>[^"]*)")?'
)


def _ua_family(ua):
    """粗粒度 UA 族：只保留"谁在调"，不保留可用于追踪的长串。"""
    if not ua:
        return "(none)"
    low = ua.lower()
    for key, label in (
        ("postman", "Postman"),
        ("python-requests", "python-requests"),
        ("curl", "curl"),
        ("okhttp", "OkHttp(Android)"),
        ("dart", "Dart/Flutter"),
        ("insomnia", "Insomnia"),
        ("axios", "axios"),
        ("mozilla", "Browser"),
    ):
        if key in low:
            return label
    return low[:32]


def _ip_token(host):
    """来源区分：不可逆短哈希（不保留原始 IP）。"""
    return hashlib.sha256(host.encode("utf-8", "replace")).hexdigest()[:8]


def parse_line(line):
    """解析一行日志 → dict 或 None。"""
    line = line.strip()
    if not line:
        return None
    if line.startswith("{"):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        request = str(payload.get("request") or payload.get("path") or "")
        parts = request.split()
        method = parts[0].upper() if len(parts) >= 2 else ""
        path = parts[1].split("?")[0] if len(parts) >= 2 else request.split("?")[0]
        status = str(payload.get("status") or payload.get("status_code") or "")
        return {
            "host": str(payload.get("remote_addr") or payload.get("host") or "unknown"),
            "time": str(payload.get("time") or payload.get("timestamp") or ""),
            "method": method,
            "path": path,
            "status": status,
            "ua": str(payload.get("http_user_agent") or payload.get("user_agent") or ""),
        }
    match = COMBINED.match(line)
    if not match:
        return None
    path = match.group("path").split("?")[0]
    return {
        "host": match.group("host"),
        "time": match.group("time"),
        "method": match.group("method"),
        "path": path,
        "status": match.group("status"),
        "ua": match.group("ua") or "",
    }


def _iter_files(targets, pattern):
    for target in targets:
        if os.path.isfile(target):
            yield target
            continue
        if not os.path.isdir(target):
            continue
        regex = re.compile(pattern.replace("*", ".*") + "$")
        for root, _dirs, names in os.walk(target):
            for name in names:
                if regex.match(name):
                    yield os.path.join(root, name)


def collect(paths, *, pattern="*.log", date_filter=None, method="POST"):
    """统计访问日志。

    method 口径：`POST` 等具体方法 → 只把该方法的旧入口命中计入主口径，
    其余方法进入 `legacy_other_hits` / `legacy_by_method` 分开统计；
    `ALL` → 所有方法都计入主口径。
    """
    if method != "ALL":
        method = method.upper()
    counts = Counter()
    legacy_by_method = Counter()
    standard_by_method = Counter()
    by_day = defaultdict(int)
    by_status = Counter()
    by_ua = Counter()
    sources = set()
    total_lines = 0
    parsed_lines = 0
    for path in _iter_files(paths, pattern):
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                total_lines += 1
                row = parse_line(line)
                if not row:
                    continue
                parsed_lines += 1
                if row["path"] not in (LEGACY_PATH, STANDARD_PATH):
                    continue
                kind = "legacy" if row["path"] == LEGACY_PATH else "standard"
                in_scope = method == "ALL" or row["method"] == method
                if kind == "legacy":
                    legacy_by_method[row["method"] or "(unknown)"] += 1
                else:
                    standard_by_method[row["method"] or "(unknown)"] += 1
                if kind == "standard":
                    counts["standard_hits"] += 1
                elif in_scope:
                    counts["legacy_hits"] += 1
                else:
                    counts["legacy_other_hits"] += 1
                if kind != "legacy" or not in_scope:
                    continue
                by_status[row["status"] or "(unknown)"] += 1
                by_day[row["time"][:11].strip() or "(unknown)"] += 1
                by_ua[_ua_family(row["ua"])] += 1
                sources.add(_ip_token(row["host"]))
    return {
        "total_lines": total_lines,
        "parsed_lines": parsed_lines,
        "method_filter": method,
        "legacy_hits": counts["legacy_hits"],
        "legacy_other_hits": counts["legacy_other_hits"],
        "legacy_by_method": dict(legacy_by_method.most_common()),
        "standard_hits": counts["standard_hits"],
        "standard_by_method": dict(standard_by_method.most_common()),
        "legacy_by_status": dict(sorted(by_status.items())),
        "legacy_by_day": dict(sorted(by_day.items())),
        "legacy_by_ua": dict(by_ua.most_common()),
        "distinct_sources": len(sources),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Walk-in 旧入口访问统计（410 观察）")
    parser.add_argument("paths", nargs="*", default=["logs"], help="日志文件或目录")
    parser.add_argument("--pattern", default="*.log", help="目录模式下匹配的文件名")
    parser.add_argument("--date", default=None, help="只统计时间列包含该子串的行")
    parser.add_argument(
        "--method",
        default="POST",
        help="旧入口主统计口径的 HTTP 方法（默认 POST；ALL 表示全方法）",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    result = collect(
        args.paths or ["logs"],
        pattern=args.pattern,
        date_filter=args.date,
        method=args.method,
    )
    if result["parsed_lines"] == 0:
        print("无可解析日志行：不能据此推断旧入口零调用（请提供部署侧访问日志）。")
        return 2
    other = ", ".join(
        f"{m} {n}" for m, n in result["legacy_by_method"].items() if m != result["method_filter"]
    )
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"扫描行数 {result['total_lines']}，可解析 {result['parsed_lines']}"
            f"（旧入口主口径：{result['method_filter']}）"
        )
        print(
            f"旧入口 {LEGACY_PATH} {result['method_filter']} 命中 {result['legacy_hits']} 次；"
            f"其他方法合计 {result['legacy_other_hits']} 次" + (f"（{other}）" if other else "")
        )
        print(
            f"标准入口 {STANDARD_PATH} 命中 {result['standard_hits']} 次"
            f"（按方法：{result['standard_by_method'] or '（无命中）'}）"
        )
        print(
            f"旧入口 {result['method_filter']} 状态码分布：{result['legacy_by_status'] or '（无命中）'}"
        )
        print(f"旧入口 {result['method_filter']} 按日：{result['legacy_by_day'] or '（无命中）'}")
        print(
            f"旧入口 {result['method_filter']} 调用方 UA 族：{result['legacy_by_ua'] or '（无命中）'}"
        )
        print(
            f"旧入口 {result['method_filter']} 不同来源数（IP 短哈希去重）：{result['distinct_sources']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
