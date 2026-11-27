"""创建并初始化取证用的隔离库（默认 test_hotel），供 E2E / 九岗位矩阵使用。

背景：PMS2 的 E2E 全链与九岗位拒绝矩阵都要求跑在**隔离库**上，开发库只作
只读种子来源。此前每轮取证都要临场手搓「建库 → migrate → seed」三步，且容易
误连开发库——2026-11-26 曾因启动器覆盖 `DJANGO_DB_NAME` 导致业务数据写进开发库
`hotel`。本脚本把这条纪律固化为一条命令。

用法（仓库根）：
    python scripts/acceptance/prepare_isolated_db.py
    python scripts/acceptance/prepare_isolated_db.py --db-name test_hotel_e2e

⚠️ 库名与 Django 测试库同名（2026-11-27 实测踩坑）：Django 的 MySQL 测试库名 =
``test_`` + 开发库名，开发库叫 ``hotel`` 时 Django 的测试库就叫 ``test_hotel``——
与本脚本的默认隔离库**同名**。因此任何一次 ``manage.py test`` 都会在收尾阶段把
这个隔离库删掉（本轮实测确实发生过一次）。

处置口径：**每次取证前先跑本脚本重建隔离库**（migrate + seed 一并完成），
不要把隔离库当作可长期留存的资产。替代方案是给应用账号授予一个不撞名的库
（如 ``test_hotel_e2e``）并统一改用该名——需要 DBA 侧一次授权，留作后续事项。

纪律：
  - 只 `CREATE DATABASE IF NOT EXISTS`，绝不 DROP / TRUNCATE 任何库；
  - 迁移与种子数据都指向隔离库；脚本启动即打印实际所连库；
  - 目标库名必须包含 test，否则拒绝执行（防止误建/误连生产库）。
"""

import argparse
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)


def read_env():
    """从 .env 读取变量（跳过平台管理员白名单，它会致身份/权限判定失真）。"""
    data = {}
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return data
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key in {"DJANGO_PLATFORM_ADMIN_USERNAME", "DJANGO_PLATFORM_ADMIN_PASSWORD"}:
                continue
            data[key] = value.strip().strip('"').strip("'")
    return data


def main():
    parser = argparse.ArgumentParser(description="创建并初始化隔离取证库")
    parser.add_argument("--db-name", default="test_hotel", help="隔离库名（必须含 test）")
    parser.add_argument("--rooms-per-type", type=int, default=3)
    parser.add_argument("--guests", type=int, default=5)
    parser.add_argument("--reservations", type=int, default=2)
    args = parser.parse_args()

    if "test" not in args.db_name.lower():
        raise SystemExit("拒绝执行：隔离库名必须包含 test，实际 %s" % args.db_name)
    if args.db_name.lower() == "test_hotel":
        print(
            "[WARN] 库名 test_hotel 与 Django 的 MySQL 测试库同名"
            "（test_ + 开发库 hotel）：任何一次 manage.py test 都会在收尾阶段删掉它。"
            "请在每次 E2E / 九岗位取证前先跑本脚本重建隔离库。"
        )

    env = read_env()
    for key in ("DJANGO_DB_USER", "DJANGO_DB_PASSWORD", "DJANGO_DB_HOST"):
        if not env.get(key):
            raise SystemExit("缺少 %s：请在 .env 中配置（脚本内禁止凭据字面量）" % key)

    import pymysql  # noqa: E402

    conn = pymysql.connect(
        user=env["DJANGO_DB_USER"],
        password=env["DJANGO_DB_PASSWORD"],
        host=env["DJANGO_DB_HOST"],
        charset="utf8mb4",
    )
    cur = conn.cursor()
    cur.execute(
        "CREATE DATABASE IF NOT EXISTS `{db}` CHARACTER SET utf8mb4 "
        "COLLATE utf8mb4_unicode_ci".format(db=args.db_name)
    )
    conn.commit()
    cur.execute("SHOW DATABASES")
    print("[DB] schemas: %s" % [row[0] for row in cur.fetchall()])
    conn.close()

    python = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    child = dict(os.environ)
    child.update(env)
    child["DJANGO_ENV"] = "dev"
    child["DJANGO_DB_NAME"] = args.db_name
    child["PYTHONIOENCODING"] = "utf-8"

    def run(argv, label):
        print("[RUN] %s" % label)
        proc = subprocess.run(
            argv, cwd=ROOT, env=child, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        for line in (proc.stdout or "").strip().splitlines()[-6:]:
            print("   ", line[:170])
        if proc.returncode != 0:
            print((proc.stderr or "")[-1500:])
            raise SystemExit("[FAIL] %s exit=%s" % (label, proc.returncode))
        print("[OK] %s" % label)

    run([python, "manage.py", "migrate", "--noinput"], "migrate")
    run(
        [
            python, "manage.py", "seed_hotel_data",
            "--rooms-per-type", str(args.rooms_per_type),
            "--guests", str(args.guests),
            "--reservations", str(args.reservations),
        ],
        "seed_hotel_data",
    )
    print("[READY] isolated db = %s" % args.db_name)


if __name__ == "__main__":
    main()
