"""Walk-in 旧入口（410）访问统计（V12-09）。

用途：在部署侧访问日志上统计旧入口 `POST /api/v1/front-desk/walk-in` 的
真实命中情况，用于"观察 → 迁移 → 410 → 退役"的收口判定。开发/本地证据目录
没有生产日志，因此本工具只负责"能算准"，样本由部署侧提供。

支持两种输入：
  * 常见 Web 服务器 combined/common 访问日志（nginx/Apache/IIS 导出均可）；
  * 单行 JSON（如 `{"request": "POST /api/v1/front-desk/walk-in", "status": 410}`）。

隐私约束：输出只含聚合计数与 User-Agent 族，**不输出客户端 IP**（只输出
不可逆短哈希用于区分来源数量），不输出 cookie / Authorization / 请求体。

用法：
    python scripts/walkin_410_stats.py access.log
    python scripts/walkin_410_stats.py access.log --json
    python scripts/walkin_410_stats.py logs/ --pattern "access*.log"

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
    r'^(?P<host>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] '
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


def collect(paths, *, pattern="*.log", date_filter=None):
    counts = Counter()
    by_day = defaultdict(Counter)
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
                if date_filter and date_filter not in row["time"]:
                    continue
                kind = "legacy" if row["path"] == LEGACY_PATH else "standard"
                counts[kind] += 1
                by_status[(kind, row["status"])] += 1
                by_day[row["time"][:11].strip() or "(unknown)"][kind] += 1
                if kind == "legacy":
                    by_ua[_ua_family(row["ua"])] += 1
                    sources.add(_ip_token(row["host"]))
    return {
        "total_lines": total_lines,
        "parsed_lines": parsed_lines,
        "legacy_hits": counts["legacy"],
        "standard_hits": counts["standard"],
        "legacy_by_status": {
            status: number
            for (kind, status), number in sorted(by_status.items())
            if kind == "legacy"
        },
        "legacy_by_day": {
            day: counter["legacy"] for day, counter in sorted(by_day.items())
        },
        "legacy_by_ua": dict(by_ua.most_common()),
        "distinct_sources": len(sources),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Walk-in 旧入口访问统计（410 观察）")
    parser.add_argument("paths", nargs="*", default=["logs"], help="日志文件或目录")
    parser.add_argument("--pattern", default="*.log", help="目录模式下匹配的文件名")
    parser.add_argument("--date", default=None, help="只统计时间列包含该子串的行")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    result = collect(args.paths or ["logs"], pattern=args.pattern, date_filter=args.date)
    if result["parsed_lines"] == 0:
        print("无可解析日志行：不能据此推断旧入口零调用（请提供部署侧访问日志）。")
        return 2
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"扫描行数 {result['total_lines']}，可解析 {result['parsed_lines']}")
        print(f"旧入口 {LEGACY_PATH} 命中 {result['legacy_hits']} 次；"
              f"标准入口 {STANDARD_PATH} 命中 {result['standard_hits']} 次")
        print(f"旧入口状态码分布：{result['legacy_by_status'] or '（无命中）'}")
        print(f"旧入口按日：{result['legacy_by_day'] or '（无命中）'}")
        print(f"旧入口调用方 UA 族：{result['legacy_by_ua'] or '（无命中）'}")
        print(f"旧入口不同来源数（IP 短哈希去重）：{result['distinct_sources']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
