"""创建并初始化取证用的隔离库（默认 test_hotel_e2e），供 E2E / 九岗位矩阵使用。

背景：PMS2 的 E2E 全链与九岗位拒绝矩阵都要求跑在**隔离库**上，开发库只作
只读种子来源。此前每轮取证都要临场手搓「建库 → migrate → seed」三步，且容易
误连开发库——2026-11-26 曾因启动器覆盖 `DJANGO_DB_NAME` 导致业务数据写进开发库
`hotel`。本脚本把这条纪律固化为一条命令。

用法（仓库根）：
    python scripts/acceptance/prepare_isolated_db.py
    python scripts/acceptance/prepare_isolated_db.py --db-name test_hotel_e2e

库名口径（2026-11-27 改名）：
  - Django 的 MySQL **测试库名 = ``test_`` + 开发库名**；开发库叫 ``hotel`` 时，
    Django 的测试库就叫 ``test_hotel``。
  - 取证隔离库此前也叫 ``test_hotel`` → 与 Django 测试库**同名** → 任何一次
    ``manage.py test`` 都会在收尾阶段把这个隔离库删掉（2026-11-27 实测发生过一次）。
  - 本脚本的默认隔离库改为 ``test_hotel_e2e``：含 ``test``（沿用本仓「库名必须含
    test」的守卫）、不等于 ``test_hotel``、且不被 ``scripts/e2e_full_chain.py``
    的守卫拒绝（该守卫拒绝 ``hotel`` / ``*_dev`` / ``prod*``）。
  - **``test_hotel`` 是 Django 的测试库名，不得再用作隔离库**；本脚本在该库名
    下会打印告警并要求改用 ``test_hotel_e2e``。

DBA 授权前置（新隔离库需要 root 侧一次授权，应用账号无建库权限）：
    CREATE DATABASE IF NOT EXISTS `test_hotel_e2e` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
    GRANT ALL PRIVILEGES ON `test_hotel_e2e`.* TO 'hotelhotel'@'127.0.0.1';
    FLUSH PRIVILEGES;
未授权时本脚本会原样打印上面这段并以非零码退出（不抛裸异常）。

纪律：
  - 只 `CREATE DATABASE IF NOT EXISTS`，绝不 DROP / TRUNCATE 任何库；
  - 迁移与种子数据都指向隔离库；脚本启动即打印实际所连库；
  - 目标库名必须包含 test，否则拒绝执行（防止误建/误连生产库）；
  - 读 `.env` 用 **setdefault 语义**（已显式设置的变量不得被 `.env` 覆盖）。
"""

import argparse
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# 取证隔离库默认名：含 test、不等于 Django 测试库名 test_hotel、且不被
# scripts/e2e_full_chain.py 的守卫拒绝。
DEFAULT_DB_NAME = "test_hotel_e2e"
# Django 的 MySQL 测试库名 = test_ + 开发库名（开发库 hotel -> test_hotel）。
DJANGO_TEST_DB_NAME = "test_hotel"
# 应用账号（只授 hotel / test_hotel），新隔离库需 DBA 侧一次授权。
GRANT_TEMPLATE = (
    "CREATE DATABASE IF NOT EXISTS `{db}` CHARACTER SET utf8mb4 "
    "COLLATE utf8mb4_unicode_ci;\n"
    "GRANT ALL PRIVILEGES ON `{db}`.* TO 'hotelhotel'@'127.0.0.1';\n"
    "FLUSH PRIVILEGES;"
)


def read_env():
    """取数据库连接变量：进程环境优先，.env 只补未显式设置的键。

    - 跳过平台管理员白名单（它会致身份/权限判定失真）；
    - setdefault 语义：调用方显式设置过的变量不得被 .env 覆盖；
    - .env 不存在时（如在 git worktree 里）不报错，退回只用进程环境变量。
    """
    data = {}
    for key, value in os.environ.items():
        if key in {"DJANGO_PLATFORM_ADMIN_USERNAME", "DJANGO_PLATFORM_ADMIN_PASSWORD"}:
            continue
        data[key] = value
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
            if key in os.environ:
                continue  # 显式设置过的变量不被 .env 覆盖
            data[key] = value.strip().strip('"').strip("'")
    return data


def error_code(exc):
    """取 MySQL 错误码（pymysql 的 args[0]）。"""
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def print_grant_hint(db_name):
    """打印可执行的 DBA 授权语句（应用账号无建库权限时唯一出路）。"""
    print("")
    print("[FAIL] 应用账号对隔离库 %r 缺少库级权限（无法 CREATE DATABASE / 建表）。" % db_name)
    print("[FAIL] 取证隔离库需要 DBA 侧一次授权。用 MySQL root 执行：")
    print("")
    print(GRANT_TEMPLATE.format(db=db_name))
    print("")
    print("[FAIL] 授权后重跑本脚本；授权前不得改用其他库名绕过（会丢失证据可复现性）。")


def main():
    # 控制台默认 GBK：授权提示含中文，统一以 UTF-8 输出（重定向到日志文件时可读）。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="创建并初始化隔离取证库")
    parser.add_argument(
        "--db-name", default=DEFAULT_DB_NAME,
        help="隔离库名（必须含 test；建议 %s，不得用 %s）"
             % (DEFAULT_DB_NAME, DJANGO_TEST_DB_NAME),
    )
    parser.add_argument("--rooms-per-type", type=int, default=3)
    parser.add_argument("--guests", type=int, default=5)
    parser.add_argument("--reservations", type=int, default=2)
    args = parser.parse_args()

    if "test" not in args.db_name.lower():
        raise SystemExit("拒绝执行：隔离库名必须包含 test，实际 %s" % args.db_name)
    if args.db_name.lower() == DJANGO_TEST_DB_NAME:
        print(
            "[WARN] 库名 test_hotel 与 Django 的 MySQL 测试库同名"
            "（test_ + 开发库 hotel）：任何一次 manage.py test 都会在收尾阶段删掉它。"
        )
        print(
            "[WARN] 请改用隔离库 %s："
            "python scripts/acceptance/prepare_isolated_db.py --db-name %s"
            % (DEFAULT_DB_NAME, DEFAULT_DB_NAME)
        )

    env = read_env()
    for key in ("DJANGO_DB_USER", "DJANGO_DB_PASSWORD", "DJANGO_DB_HOST"):
        if not env.get(key):
            raise SystemExit("缺少 %s：请在 .env 中配置（脚本内禁止凭据字面量）" % key)

    # 启动即打印实际所连库（人工核对，防止隔离库被静默替换成开发库）。
    print("[DB] target=%s host=%s port=%s env=dev"
          % (args.db_name, env["DJANGO_DB_HOST"], env.get("DJANGO_DB_PORT", "3306")))

    import pymysql  # noqa: E402

    try:
        conn = pymysql.connect(
            user=env["DJANGO_DB_USER"],
            password=env["DJANGO_DB_PASSWORD"],
            host=env["DJANGO_DB_HOST"],
            port=int(env.get("DJANGO_DB_PORT", "3306")),
            charset="utf8mb4",
        )
    except pymysql.MySQLError as exc:
        code = error_code(exc)
        if code == 1045:
            print("[FAIL] MySQL 认证失败（1045）：.env 的 DJANGO_DB_USER / DJANGO_DB_PASSWORD "
                  "与服务端不一致（脚本内禁止凭据字面量，请在 .env 修正）。")
            raise SystemExit(2)
        if code in (1044, 1142):
            print_grant_hint(args.db_name)
            raise SystemExit(2)
        print("[FAIL] MySQL 连接失败：%s" % exc)
        raise SystemExit(2)

    cur = conn.cursor()
    try:
        cur.execute(
            "CREATE DATABASE IF NOT EXISTS `{db}` CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_unicode_ci".format(db=args.db_name)
        )
    except pymysql.MySQLError as exc:
        code = error_code(exc)
        conn.rollback()
        if code in (1044, 1142):
            print_grant_hint(args.db_name)
            raise SystemExit(2)
        print("[FAIL] 建库失败（%s）：%s" % (args.db_name, exc))
        raise SystemExit(2)
    conn.commit()
    cur.execute("SHOW DATABASES")
    print("[DB] schemas: %s" % [row[0] for row in cur.fetchall()])
    conn.close()

    python = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    if not os.path.exists(python):
        # worktree 无 .venv 时退回当前解释器（调用方通常已用主仓虚拟环境启动本脚本）。
        python = sys.executable
    print("[RUN] python=%s" % python)

    child = dict(os.environ)
    for key, value in env.items():
        child.setdefault(key, value)  # setdefault 语义：不得覆盖调用方显式设置的值
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
            tail = (proc.stderr or "")[-1500:]
            print(tail)
            if "1044" in tail or "1142" in tail or "Access denied" in tail:
                print_grant_hint(args.db_name)
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
