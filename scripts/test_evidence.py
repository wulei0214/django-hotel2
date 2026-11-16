"""测试证据生成器（R1-05）：运行测试并在证据文件头写入可审计元数据。

用法（仓库根）：
  python scripts/test_evidence.py --engine sqlite --output <evidence.log> [--keepdb]
  python scripts/test_evidence.py --engine mysql  --output <evidence.log> [--keepdb]

头记录：时间戳 / Git SHA / Python / Django / 数据库类型与版本 / 执行命令 /
DJANGO_ENV / 测试总数-通过-失败-跳过（从输出解析）/ 退出码。
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _git_state():
    """被测树绑定：HEAD SHA + 未提交变更检测（含暂存）。

    证据 SHA 必须能回答"测的是不是这个提交的树"——工作区不干净时如实标注，
    避免"证据 SHA != 被测代码"的审计缺口。"""
    try:
        sha = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=10
            ).stdout.strip()
            or "unknown"
        )
        st = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True, timeout=10
        ).stdout
        dirty = len([line for line in st.splitlines() if line.strip()])
        if dirty:
            return "%s（工作区含未提交变更 %d 项：证据树不等于该提交，需以本行为准）" % (sha, dirty)
        return sha
    except Exception:
        return "unknown"


def _mysql_conn_kwargs():
    """MySQL 连接参数只从环境变量读取（先 set -a && source .env && set +a）。

    不得在脚本内写凭据字面量（AGENTS.md 会话纪律）；缺失即快速失败。"""
    keys = ("DJANGO_DB_USER", "DJANGO_DB_PASSWORD", "DJANGO_DB_HOST", "DJANGO_DB_NAME")
    cfg = {k: os.environ.get(k) for k in keys}
    missing = [k for k in keys if not cfg[k]]
    if missing:
        raise RuntimeError(
            "MySQL 引擎缺少环境变量："
            + "、".join(missing)
            + "；请先 set -a && source .env && set +a（脚本内禁止凭据字面量）"
        )
    return {
        "user": cfg["DJANGO_DB_USER"],
        "password": cfg["DJANGO_DB_PASSWORD"],
        "host": cfg["DJANGO_DB_HOST"],
        "database": cfg["DJANGO_DB_NAME"],
    }


def _db_version(engine):
    try:
        import pymysql

        conn = pymysql.connect(**_mysql_conn_kwargs())
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

    env = dict(**__import__("os").environ)
    if args.engine == "sqlite":
        env.update({"DJANGO_DB_ENGINE": "sqlite", "DJANGO_ENV": "test"})
        db_desc = "SQLite（内存库，CI/轻量口径）"
    else:
        _mysql_conn_kwargs()  # 预校验：缺环境变量在跑测试前快速失败
        env.update({"DJANGO_ENV": "test"})  # DB 连接参数由环境透传，不在脚本内覆写
        db_desc = _db_version("mysql")
        if args.keepdb:
            env["DJANGO_KEEPDB"] = "1"

    test_cmd = [sys.executable, "manage.py", "test", "--noinput"]
    # --noinput：测试库一律全新创建（陈旧 keepdb 库会缺新迁移/种子数据；
    # 交互确认在无人值守下必撞 EOF）。需要复用库时显式走 manage.py test --keepdb。
    if args.keepdb:
        test_cmd.append("--keepdb")  # keepdb 必须进 manage.py 参数（env DJANGO_KEEPDB 非 settings 约定）
    header = [
        f"证据生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"被测树：{_git_state()}",
        f"Python：{sys.version.split()[0]}    Django：{django.get_version()}",
        f"数据库：{db_desc}",
        f"执行命令：{' '.join(test_cmd)}（DJANGO_ENV=test，engine={args.engine}）",
    ]

    proc = subprocess.run(
        test_cmd,
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
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
