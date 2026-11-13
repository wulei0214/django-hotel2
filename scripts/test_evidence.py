# -*- coding: utf-8 -*-
"""测试证据生成器（R1-05）：运行测试并在证据文件头写入可审计元数据。

用法（仓库根）：
  python scripts/test_evidence.py --engine sqlite --output <evidence.log> [--keepdb]
  python scripts/test_evidence.py --engine mysql  --output <evidence.log> [--keepdb]

头记录：时间戳 / Git SHA / Python / Django / 数据库类型与版本 / 执行命令 /
DJANGO_ENV / 测试总数-通过-失败-跳过（从输出解析）/ 退出码。
"""
import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _git_sha():
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True,
            timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _db_version(engine):
    try:
        import pymysql
        conn = pymysql.connect(
            user="hotelhotel", password="hotelhotel", host="localhost", database="hotel")
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
        conn.close()
        return f"MySQL {version}"
    except Exception as exc:
        return f"{engine}（版本查询失败：{exc}）"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=["sqlite", "mysql"], required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--keepdb", action="store_true")
    args = parser.parse_args()

    import django
    from django.conf import settings

    env = dict(**__import__("os").environ)
    if args.engine == "sqlite":
        env.update({"DJANGO_DB_ENGINE": "sqlite", "DJANGO_ENV": "test"})
        db_desc = "SQLite（内存库，CI/轻量口径）"
    else:
        env.update({
            "DJANGO_DB_USER": "hotelhotel", "DJANGO_DB_PASSWORD": "hotelhotel",
            "DJANGO_DB_NAME": "hotel", "DJANGO_DB_HOST": "localhost",
            "DJANGO_ENV": "test",
        })
        db_desc = _db_version("mysql")
        if args.keepdb:
            env["DJANGO_KEEPDB"] = "1"

    test_cmd = [sys.executable, "manage.py", "test"]
    header = [
        f"证据生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"被测 Git SHA：{_git_sha()}",
        f"Python：{sys.version.split()[0]}    Django：{django.get_version()}",
        f"数据库：{db_desc}",
        f"执行命令：{' '.join(test_cmd)}（DJANGO_ENV=test，engine={args.engine}）",
    ]

    proc = subprocess.run(
        test_cmd, cwd=REPO, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    output = (proc.stdout or "") + (proc.stderr or "")

    ran = re.findall(r"Ran (\d+) tests?", output)
    ok = "OK" if re.search(r"^OK\b", output, re.M) else ""
    failed = re.search(r"^FAILED \((.*)\)", output, re.M)
    verdict = ok if ok else (f"FAILED ({failed.group(1)})" if failed else "无最终判定（异常中断）")
    skips = re.search(r"skipped=(\d+)", output)
    header += [
        f"测试总数：{ran[-1] if ran else '未知'}    最终判定：{verdict}"
        + (f"    跳过：{skips.group(1)}" if skips else ""),
        f"退出码：{proc.returncode}",
        "=" * 72,
    ]

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(header) + "\n" + output, encoding="utf-8")
    print("\n".join(header[:7]))
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
