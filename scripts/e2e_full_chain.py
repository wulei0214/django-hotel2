# -*- coding: utf-8 -*-
"""G14-09 全链路业务串测脚本（真实 HTTP 接口驱动，≥36 项断言，落验收证据）。

修复历史（95 号门禁报告 §7.1 判定的 8 项 P1/P2 缺陷）：
  1. 必要 imports 缺失 / 断言空转 —— 全部断言重写为对响应字段的强校验，
     无任何 `step(..., True)` 型恒真断言；
  2. 凭据硬编码 —— 数据库凭据读 .env / 环境变量，登录口令只经
     PMS_E2E_PASSWORD 注入，脚本内零口令零密钥；
  3. 清库目标保护不足 —— 四重护栏（见下）；
  4. F1/F2 空断言 —— F1 改为断言通知中心派生的具体业务工作项
     （待确认预订/今日预抵含预订号），F2 修正审计动作名为 night_audit.run
     （旧脚本查询 night.audit_run 永远空集）；
  5. E4 允许 HTTP 200 —— 严格断言 403 且错误封套 code=PERMISSION_DENIED；
  6. D2 允许未结清退房返回成功 —— 严格断言 409 + 应收余额金额解析 + 补收；
  7. B 列表 keyword 参数失效 —— 修正为 q（P0-03 统一分页/检索契约）；
  8. 36/36 不被接受 —— 本版按当前 API 契约（apps/api/urls.py @ main）逐项
     对齐并扩展到 50+ 项断言，全部真实 HTTP 驱动。

清库四重护栏（缺一拒绝执行，退出码 2）：
  a. 操作者必须显式传入 --i-understand-reset（无参数直接拒绝）；
  b. DJANGO_ENV 必须为 dev 且 settings.DEBUG=True（仅本机开发环境）；
  c. 数据库主机必须 loopback，库名必须显式列入 PMS_E2E_ALLOWED_DB_NAMES
     白名单且以 _e2e/_test 结尾（hotel / *_dev 一律拒绝），且与 Django
     default 连接逐项一致（MySQL）；
  d. 执行前打印将要 TRUNCATE 的全部表名（清空范围）与保留表清单；
  e. 库名撞名告警：test_hotel 是 Django 的 MySQL 测试库名（test_ + 开发库 hotel），
     会被任何一次 manage.py test 的收尾阶段删除——撞名影响的是隔离库的复用而非
     本次取证的正确性，故为告警而非拒绝；目标状态是改用 test_hotel_e2e。

隔离库自建数据：migrate → append-only 触发器（权限边界内尽力安装）→
清库复位（打印清空范围）→ 主数据复刻（从 dev 基线库只读复制九角色授权
矩阵/员工账号/房型房价计划；库存日历按今天重建 30 天窗口）→
seed_hotel_data → FIRST_NIGHT 提前离店政策（使退房链的「未结清拒绝→
补收→两讫」在同日退房下可确定性触发）→ seed_role_test_accounts
（口令取 PMS_E2E_PASSWORD，不硬编码）。

运行方式（本机隔离库）：
  DJANGO_DB_NAME=test_hotel_e2e \
  PMS_E2E_ALLOWED_DB_NAMES=test_hotel_e2e \
  PMS_E2E_PASSWORD='<九角色统一测试口令>' \
  PMS_E2E_BASE_URL=http://127.0.0.1:8001 \
  python scripts/e2e_full_chain.py --i-understand-reset

链路：公开预订（PENDING cap=2）→ 模拟支付确认 → 前台列表新字段 →
押金 30% 服务端强制（PENDING 收款即确认）→ 排房 → 入住 → 在住收款 →
Walk-in 双端点（/walk-ins 标准 + /walk-in 并存）→ 夜审（wb 守卫只审当前
营业日；run 关账；CLOSED 拒写；重开授权链 SoD）→ 退房（拒绝未结清→补收
→两讫；提前离店净额结算无双记）→ 清洁链（DIRTY→CLEANING→INSPECTING→
服务员放行 403→经理放行 CLEAN）→ 退款三分离（申请/审批/执行+幂等键头
必带+SoD 反例）→ 互通断言（通知/审计/报表/顾客侧令牌/账本 append-only/
BillingInputError 400/409 分流）。
"""
import argparse
import getpass
import json
import os
import re
import sys
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_EVIDENCE = "docs/acceptance/evidence/e2e-rerun-20261116.log"


def _parse_args():
    parser = argparse.ArgumentParser(
        description="PMS 全链路 HTTP E2E（清空指定隔离测试库交易数据并自建基线）")
    parser.add_argument(
        "--i-understand-reset", action="store_true",
        help="显式确认清空已校验的本地隔离测试库（无此参数一律拒绝执行）")
    parser.add_argument(
        "--evidence", default=DEFAULT_EVIDENCE,
        help="证据文件路径（默认 docs/acceptance/evidence/e2e-rerun-20261116.log）")
    parser.add_argument(
        "--skip-reset", action="store_true",
        help="跳过清库复位（仅当目标库刚被本脚本初始化过时允许；仍要求全部护栏）")
    return parser.parse_args()


ARGS = _parse_args()


def _load_dotenv():
    """读取仓库 .env（Django 不自动读取）；shell 已有变量优先，不覆盖。"""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()

# ---------------------------------------------------------------------------
# 清库护栏（全部通过才允许触碰数据库）
# ---------------------------------------------------------------------------
if not ARGS.i_understand_reset:
    print("拒绝执行：清库复位必须显式传入 --i-understand-reset 参数。")
    print("护栏将打印将要清空（TRUNCATE）的交易表范围与保留的主数据表清单；")
    print("目标库仅允许本机 DJANGO_ENV=dev + 白名单 *_e2e/*_test 隔离库。")
    sys.exit(2)

if os.environ.get("DJANGO_ENV", "").strip().lower() != "dev":
    print("拒绝执行：清库复位仅允许 DJANGO_ENV=dev（当前 %r）。"
          % os.environ.get("DJANGO_ENV", ""))
    sys.exit(2)

_REQUIRED_DB_ENV = ("DJANGO_DB_HOST", "DJANGO_DB_NAME", "DJANGO_DB_USER",
                    "DJANGO_DB_PASSWORD", "DJANGO_DB_PORT")
for _required in _REQUIRED_DB_ENV:
    if not os.environ.get(_required, "").strip():
        print(f"拒绝执行：缺少数据库配置 {_required}（从 .env 或环境变量读取）。")
        sys.exit(2)

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_DB_HOST = os.environ["DJANGO_DB_HOST"].strip().lower()
_DB_NAME = os.environ["DJANGO_DB_NAME"].strip()
_DB_PORT = os.environ["DJANGO_DB_PORT"].strip()
if _DB_HOST not in _LOOPBACK:
    print("拒绝执行：E2E 清库只允许连接本机（loopback）数据库主机。")
    sys.exit(2)
_ALLOWED_DB_NAMES = {
    item.strip() for item in os.environ.get("PMS_E2E_ALLOWED_DB_NAMES", "").split(",")
    if item.strip()
}
if not _ALLOWED_DB_NAMES or _DB_NAME not in _ALLOWED_DB_NAMES:
    print("拒绝执行：目标数据库必须显式列入 PMS_E2E_ALLOWED_DB_NAMES 白名单"
          f"（当前白名单 {sorted(_ALLOWED_DB_NAMES) or '为空'}）。")
    sys.exit(2)
if _DB_NAME == "hotel" or _DB_NAME.endswith("_dev") or _DB_NAME.lower().startswith("prod"):
    print("拒绝执行：通用开发库名（hotel / *_dev）不视为可清库证明。")
    sys.exit(2)
_IS_ISOLATED_NAME = _DB_NAME.endswith(("_e2e", "_test")) or _DB_NAME.startswith("test_")
if not _IS_ISOLATED_NAME:
    print("拒绝执行：清库目标数据库名必须以 _e2e/_test 结尾或以 test_ 开头（隔离库命名）。")
    sys.exit(2)
# 库名撞名告警：Django 的 MySQL 测试库名 = test_ + 开发库名（hotel -> test_hotel）。
# 取证隔离库若与它同名，任何一次 manage.py test 的收尾阶段都会把隔离库删掉。
#
# 口径：**告警而非拒绝**（2026-11-27 复核订正）。理由：
#   * 撞名影响的是「隔离库的**复用**」，不是「本次 E2E 的正确性」——只要按
#     `prepare_isolated_db.py` 在每次取证前重建隔离库、且不在「重建」与「使用」
#     之间跑 manage.py test，本次 E2E 取证完全有效；
#   * 早期版本做成硬拒绝，结果是**在 DBA 侧授权落地前把 E2E 能力整体堵死**
#     （授权未落地时无任何可跑的隔离库名）——这属于能力回退，不是纠错；
#   * 目标状态仍是改名后的 `test_hotel_e2e`：默认库名、取证工具与
#     `PMS_E2E_ALLOWED_DB_NAMES` 都已切到它；下面的告警同时打印需要 DBA
#     执行的授权语句，授权落地后旧名不再需要。
if _DB_NAME == "test_hotel":
    print("=" * 72)
    print("[WARN] 库名 test_hotel 与 Django 的 MySQL 测试库同名（test_ + 开发库 hotel）。")
    print("       任何一次 manage.py test 的收尾阶段都会删掉这个库（2026-11-27 实测发生过）。")
    print("       本次 E2E 取证仍有效——前提是隔离库已由 prepare_isolated_db.py 重建，")
    print("       且在本次取证结束前不要再跑 manage.py test。")
    print("       目标状态是改用不撞名的隔离库 test_hotel_e2e，需 DBA 先执行：")
    print("         CREATE DATABASE IF NOT EXISTS `test_hotel_e2e` CHARACTER SET utf8mb4 "
          "COLLATE utf8mb4_unicode_ci;")
    print("         GRANT ALL PRIVILEGES ON `test_hotel_e2e`.* TO 'hotelhotel'@'127.0.0.1';")
    print("         FLUSH PRIVILEGES;")
    print("=" * 72)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hotel.settings")
import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402

if not settings.DEBUG:
    print("拒绝执行：全链路串测（含清库）仅允许 DEBUG=True 本机环境。")
    sys.exit(2)
if getattr(settings, "DJANGO_ENV", "dev") != "dev":
    print("拒绝执行：全链路串测（含清库）仅允许 DJANGO_ENV=dev。")
    sys.exit(2)

_DB_CONFIG = settings.DATABASES.get("default", {})
if "mysql" not in str(_DB_CONFIG.get("ENGINE", "")).lower():
    print("拒绝执行：Django default 数据库必须是 MySQL（隔离库基线在 MySQL 建立）。")
    sys.exit(2)


def _norm_host(value):
    return "127.0.0.1" if str(value).strip().lower() == "localhost" else str(value).strip().lower()


if (
    _norm_host(_DB_CONFIG.get("HOST", "")) != _norm_host(_DB_HOST)
    or str(_DB_CONFIG.get("NAME", "")).strip() != _DB_NAME
    or str(_DB_CONFIG.get("USER", "")) != os.environ["DJANGO_DB_USER"]
    or str(_DB_CONFIG.get("PASSWORD", "")) != os.environ["DJANGO_DB_PASSWORD"]
    or str(_DB_CONFIG.get("PORT", "")).strip() != _DB_PORT
):
    print("拒绝执行：Django default 数据库连接与 DJANGO_DB_* 环境变量不完全一致，")
    print("        （清库目标必须与业务连接是同一个库，防止清错目标）。")
    sys.exit(2)

import pymysql  # noqa: E402
import requests  # noqa: E402

BASE = os.environ.get("PMS_E2E_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
_parsed_base = urlparse(BASE)
if _parsed_base.hostname not in _LOOPBACK:
    print("拒绝执行：PMS_E2E_BASE_URL 必须指向本机服务（%s）。" % BASE)
    sys.exit(2)
PWD = os.environ.get("PMS_E2E_PASSWORD", "")
if not PWD:
    print("拒绝执行：请通过 PMS_E2E_PASSWORD 注入九角色测试账号口令（不得写入脚本）。")
    sys.exit(2)

# ---------------------------------------------------------------------------
# 证据输出：stdout 镜像写入证据文件（G14-09 指定路径）
# ---------------------------------------------------------------------------
EVIDENCE_PATH = (REPO_ROOT / ARGS.evidence) if not os.path.isabs(ARGS.evidence) else Path(ARGS.evidence)
EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)


class _Tee:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for stream in self._streams:
            stream.write(data)
        return len(data)

    def flush(self):
        for stream in self._streams:
            stream.flush()


_EVIDENCE_FILE = EVIDENCE_PATH.open("w", encoding="utf-8")
sys.stdout = _Tee(sys.__stdout__, _EVIDENCE_FILE)

print("=== PMS2 G14-09 e2e_full_chain 复跑证据 ===")
print("run_started_at:", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
print("base_url:", BASE)
print("target_db:", _DB_NAME, "@", _DB_HOST, _DB_PORT, "(MySQL, 隔离测试库)")
print("django_env:", os.environ.get("DJANGO_ENV"), "DEBUG:", settings.DEBUG)
try:
    import subprocess

    _sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO_ROOT),
        capture_output=True, text=True, timeout=10).stdout.strip()
    print("repo_head:", _sha or "(unknown)")
except Exception:
    print("repo_head: (unknown)")
print("evidence_file:", str(EVIDENCE_PATH))
print()

RESULTS = []
TRIGGER_INSTALL_NOTE = ""


def step(name, ok, detail=""):
    """登记并打印一条断言结果；ok 必须来自真实响应字段的比较表达式。"""
    RESULTS.append((name, bool(ok), str(detail)))
    print(("[PASS] " if ok else "[FAIL] ") + name + ("  | " + str(detail) if detail else ""))
    return bool(ok)


def q2(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# 隔离库初始化：建库 → migrate → append-only 触发器 → 清库复位 → 种子
# ---------------------------------------------------------------------------
def _server_connect():
    return pymysql.connect(
        host=_DB_CONFIG["HOST"], user=_DB_CONFIG["USER"],
        password=_DB_CONFIG["PASSWORD"], port=int(_DB_CONFIG["PORT"]),
        charset="utf8mb4", connect_timeout=5,
    )


KEEP_TABLES = {
    "core_property", "inventory_roomtype", "inventory_room", "inventory_rateplan",
    "reservations_cancellationpolicy", "reservations_depositpolicy",
    "identity_staffprofile", "identity_propertystaffmembership",
    "identity_staffroleassignment", "identity_invitation",
    "auth_user", "auth_group", "auth_permission", "auth_user_groups",
    "auth_user_user_permissions", "auth_group_permissions",
    "django_migrations", "django_content_type",
}


def _db_cursor():
    conn = pymysql.connect(
        host=_DB_CONFIG["HOST"], user=_DB_CONFIG["USER"],
        password=_DB_CONFIG["PASSWORD"], database=_DB_CONFIG["NAME"],
        port=int(_DB_CONFIG["PORT"]), charset="utf8mb4", connect_timeout=5,
    )
    return conn


def ensure_database():
    conn = _server_connect()
    with conn.cursor() as cursor:
        cursor.execute(
            "CREATE DATABASE IF NOT EXISTS `%s` CHARACTER SET utf8mb4" % _DB_NAME)
        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
            (_DB_NAME,))
        existing = {row[0] for row in cursor.fetchall()}
    conn.close()
    return existing


def compute_reset_scope():
    conn = _db_cursor()
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_type='BASE TABLE'")
        tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    to_clear = sorted(t for t in tables if t not in KEEP_TABLES)
    kept = sorted(t for t in tables if t in KEEP_TABLES)
    return to_clear, kept


def print_reset_scope():
    to_clear, kept = compute_reset_scope()
    print("[GUARD] 清空范围（TRUNCATE，共 %d 张交易表）：" % len(to_clear))
    for name in to_clear:
        print("[GUARD]   - " + name)
    print("[GUARD] 保留范围（主数据/员工账号/迁移元数据，共 %d 张）：" % len(kept))
    for name in kept:
        print("[GUARD]   = " + name)
    return to_clear


def reset_baseline(to_clear):
    conn = _db_cursor()
    with conn.cursor() as cursor:
        cursor.execute("SET FOREIGN_KEY_CHECKS=0")
        for table in to_clear:
            cursor.execute("TRUNCATE TABLE `%s`" % table)
        cursor.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()
    conn.close()


# 从 dev 基线库复刻的主数据表（只读源库，绝不写源库）：
# 房型/房价计划/物理房、员工账号与九角色授权矩阵、政策、物业与内容类型。
# 库存日历（dailyrate/roomtypeinventory）不复制——由 seed_hotel_data 以
# 「今天」为起点重建 30 天窗口，保证可订性与业务日期一致。
COPY_SET = (
    "django_content_type",
    "auth_permission", "auth_group", "auth_group_permissions",
    "auth_user", "auth_user_groups", "auth_user_user_permissions",
    "core_property",
    "inventory_roomtype", "inventory_room", "inventory_rateplan",
    "identity_staffprofile", "identity_propertystaffmembership",
    "identity_staffroleassignment", "identity_invitation",
    "reservations_cancellationpolicy", "reservations_depositpolicy",
)


def bootstrap_main_data():
    """把 dev 基线库的主数据复刻进隔离库（GROUP/权限矩阵/员工账号/房型房价）。

    隔离库可能被并行会话清写（本复跑当日即发生 auth_group 被外 Clear 的情况），
    因此每次运行都以 dev 基线库为准重建主数据，保证角色权限矩阵与九角色
    账号在位。源库只读；目标库复制集先 TRUNCATE 再整表复制（幂等）。
    """
    source_db = os.environ.get("PMS_E2E_SOURCE_DB", "hotel").strip()
    if source_db == _DB_NAME:
        print("拒绝执行：主数据源库不能与清库目标是同一个库。")
        raise SystemExit(2)
    conn = _server_connect()
    copied = {}
    try:
        with conn.cursor() as cursor:
            cursor.execute("SET FOREIGN_KEY_CHECKS=0")
            for table in COPY_SET:
                cursor.execute("TRUNCATE TABLE `%s`.`%s`" % (_DB_NAME, table))
            for table in COPY_SET:
                cursor.execute(
                    "SELECT * FROM `%s`.`%s`" % (source_db, table))
                columns = [d[0] for d in cursor.description]
                rows = cursor.fetchall()
                if not rows:
                    copied[table] = 0
                    continue
                placeholders = ",".join(["%s"] * len(columns))
                column_list = ",".join("`%s`" % c for c in columns)
                cursor.executemany(
                    "INSERT INTO `%s`.`%s` (%s) VALUES (%s)"
                    % (_DB_NAME, table, column_list, placeholders),
                    rows)
                copied[table] = len(rows)
            # 物理房态复位（复制的是 dev 的实时房态，统一归零）
            cursor.execute(
                "UPDATE `%s`.inventory_room SET occupancy_status='VACANT', "
                "housekeeping_status='CLEAN'" % _DB_NAME)
            cursor.execute("SET FOREIGN_KEY_CHECKS=1")
        conn.commit()
    finally:
        conn.close()
    print("[BOOTSTRAP] 主数据复刻自 `%s`（只读源库）：%s"
          % (source_db, ", ".join("%s=%d" % (k, v) for k, v in copied.items())))


def provision_database():
    """建库 + 全量迁移 + append-only 触发器（隔离库自建，幂等）。

    触发器安装需要 MySQL SUPER（或 log_bin_trust_function_creators=ON）；
    本机 app 用户仅库级 ALL PRIVILEGES 且 binlog 开启 → 1419 权限边界
    （CI 的 MySQL 服务以 root 安装）。安装失败不阻断链路，F5 断言降级为
    API 只追加契约面（账本无写路由）并留痕权限边界。
    """
    existing = ensure_database()
    if existing:
        print("[PROVISION] 目标库已存在 %d 张表，执行幂等迁移校准。" % len(existing))
    call_command("migrate", interactive=False, verbosity=1)
    global TRIGGER_INSTALL_NOTE
    try:
        call_command("install_mysql_append_only_guards", verbosity=0)
        TRIGGER_INSTALL_NOTE = ""
        print("[PROVISION] migrate + append-only triggers 完成。")
    except Exception as exc:
        TRIGGER_INSTALL_NOTE = str(exc)[:200]
        print("[PROVISION] migrate 完成；append-only triggers 安装被数据库权限"
              "边界拒绝（1419，本机 app 用户无 SUPER 且 binlog 开启），"
              "F5 断言改走 API 只追加契约面。原始错误：%s" % TRIGGER_INSTALL_NOTE)


def seed_baseline():
    """清库后重建业务基线：房型/房价/库存 + FIRST_NIGHT 政策 + 九角色账号。"""
    call_command("seed_hotel_data", rooms_per_type=4, guests=0, reservations=0,
                 verbosity=0)
    from apps.inventory.models import RatePlan
    from apps.reservations.models import EarlyDeparturePolicy

    policies = 0
    for rate_plan in RatePlan.objects.all():
        _, created = EarlyDeparturePolicy.objects.get_or_create(
            rate_plan=rate_plan, policy_type=EarlyDeparturePolicy.PolicyType.FIRST_NIGHT,
            is_active=True,
            defaults={"policy_value": None})
        policies += 1 if created else 0
    print("[SEED] 房型/房价/30 天库存/净房完成；FIRST_NIGHT 提前离店政策覆盖 "
          "%d 个房价计划（新增 %d）。" % (RatePlan.objects.count(), policies))
    # 九角色测试账号：口令仅经 PMS_E2E_PASSWORD 注入 seed 命令（getpass 注入，
    # 不落盘、不入脚本）。该命令自带 dev/DEBUG/本机库护栏与密码强度校验。
    original_getpass = getpass.getpass
    getpass.getpass = lambda *a, **k: PWD
    try:
        call_command("seed_role_test_accounts", verbosity=0)
    finally:
        getpass.getpass = original_getpass
    print("[SEED] 九角色测试账号就绪（口令未输出）。")


def assert_service_targets_same_db(anon):
    """服务身份预检：:8001 服务所连库必须与本脚本/清库目标一致。

    有并行部署/双进程抢占端口时，HTTP 流量可能落到另一个库（如 dev hotel）。
    三重校验，任一不符立即终止，绝不把写链路打到非目标库：
      1. PMS_E2E_PASSWORD 登录 test_front 必须成功——该口令只写入了清库
         目标库（bootstrap/seed 阶段 set_password），dev 库口令不同；
      2. availability 房型 id 集与目标库种子一致；
      3. 夜审营业日列表必须为空（隔离库刚被复位；dev 库留有历史营业日）。
    """
    from apps.core.models import BusinessDate

    seeded_ids = sorted(_seeded_room_type_ids())
    today = date.today()
    code, d = api(anon, "GET", "/api/v1/availability?check_in=%s&check_out=%s"
                  % (today.isoformat(), (today + timedelta(days=1)).isoformat()))
    served_ids = sorted(t.get("room_type_id") for t in (d.get("data") or [])
                        if isinstance(t, dict))
    print("[IDENTITY] 目标库房型 id=%s；服务返回房型 id=%s（HTTP %d）"
          % (seeded_ids, served_ids, code))
    if code != 200 or served_ids != seeded_ids:
        print("拒绝执行：:8001 服务连接的数据库与清库目标 %s 不一致"
              "（疑似端口被另一部署抢占）。请核对部署后重跑。" % _DB_NAME)
        raise SystemExit(2)
    try:
        duty = login("test_front")
    except AssertionError as exc:
        print("拒绝执行：%s——:8001 服务连接的数据库不是已注入 PMS_E2E_PASSWORD "
              "的清库目标 %s。" % (exc, _DB_NAME))
        raise SystemExit(2)
    local_bd_count = BusinessDate.objects.count()
    code, d = api(duty, "GET", "/api/v1/night-audit/business-dates")
    served_bd = d.get("data", [])
    print("[IDENTITY] 目标库营业日行数=%d；服务返回营业日行数=%d（HTTP %d）"
          % (local_bd_count, len(served_bd), code))
    if local_bd_count != len(served_bd):
        print("拒绝执行：:8001 服务连接的数据库与清库目标 %s 不一致"
              "（营业日历史不匹配）。请核对部署后重跑。" % _DB_NAME)
        raise SystemExit(2)
    print("[IDENTITY] 服务库与清库目标一致：%s ✓" % _DB_NAME)


def _seeded_room_type_ids():
    from apps.inventory.models import RoomType

    return list(RoomType.objects.values_list("pk", flat=True))


# ---------------------------------------------------------------------------
# HTTP 工具
# ---------------------------------------------------------------------------
SESSIONS = {}


def login(username):
    """员工会话登录（口令取 PMS_E2E_PASSWORD）；按用户名缓存复用。"""
    if username in SESSIONS:
        return SESSIONS[username]
    s = requests.Session()
    r = s.get(BASE + "/login/", timeout=15)
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text)
    if not m:
        raise AssertionError(f"{username} 登录页缺少 csrf token")
    r2 = s.post(BASE + "/login/", data={
        "username": username, "password": PWD,
        "csrfmiddlewaretoken": m.group(1), "next": "/home/"},
        headers={"Referer": BASE + "/login/"}, timeout=15)
    if r2.status_code != 200 or "/login/" in r2.url:
        raise AssertionError(f"{username} 登录失败 {r2.status_code} {r2.url}")
    SESSIONS[username] = s
    return s


def anon_session():
    """匿名会话：先取 /reserve/ 建立 session 内 CSRF 事实（CSRF_USE_SESSIONS）。"""
    s = requests.Session()
    s.get(BASE + "/reserve/", timeout=15)
    return s


def csrf_of(s):
    if getattr(s, "_csrf", None):
        return s._csrf
    r = s.get(BASE + "/reserve/", timeout=15)
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r.text)
    s._csrf = m.group(1) if m else ""
    return s._csrf


def api(s, method, path, payload=None, idem=None, headers=None):
    h = {"Content-Type": "application/json"}
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        token = csrf_of(s)
        if token:
            h["X-CSRFToken"] = token
    if idem:
        h["Idempotency-Key"] = idem
    if headers:
        h.update(headers)
    r = s.request(method, BASE + path,
                  data=json.dumps(payload) if payload is not None else None,
                  headers=h, timeout=30)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:200]}


def assert_envelope(name, code, body, expect_status, expect_code=None,
                    expect_msg=None):
    """错误封套 {error:{code,message}}（MAINT-04 契约）强校验。"""
    error = body.get("error") if isinstance(body, dict) else None
    ok = (code == expect_status and isinstance(error, dict)
          and isinstance(error.get("code"), str) and error.get("code") != ""
          and isinstance(error.get("message"), str) and error.get("message") != "")
    if ok and expect_code:
        ok = error.get("code") == expect_code
    if ok and expect_msg:
        ok = expect_msg in error.get("message", "")
    step(name, ok, "" if ok else f"HTTP {code} {str(body)[:160]}")


# ---------------------------------------------------------------------------
# 主链路
# ---------------------------------------------------------------------------
def main():
    from decimal import Decimal

    # ---- 隔离库初始化（护栏已通过；先打印清空范围再执行） ----
    provision_database()
    to_clear = print_reset_scope()
    if not ARGS.skip_reset:
        reset_baseline(to_clear)
        print("[RESET] 已清空交易表 %d 张（范围见护栏清单）。" % len(to_clear))
        bootstrap_main_data()
    else:
        print("[RESET] --skip-reset：保留当前数据（护栏仍全部生效）。")
    seed_baseline()

    today = date.today()
    tomorrow = today + timedelta(days=1)
    CHECK_IN = str(today)
    CHECK_OUT = str(today + timedelta(days=3))

    # ================================================================ A 顾客链
    anon = anon_session()
    assert_service_targets_same_db(anon)
    code, d = api(anon, "GET",
                  f"/api/v1/availability?check_in={CHECK_IN}&check_out={CHECK_OUT}")
    types = [t for t in (d.get("data") or []) if isinstance(t, dict)]
    pick = next((t for t in types if (t.get("available_count") or 0) > 0), None)
    step("A1 可订查询返回可售房型（含 rate_plan/总价）",
         code == 200 and pick is not None
         and pick.get("room_type_id") and pick.get("rate_plan_id")
         and Decimal(str(pick.get("total_amount") or 0)) > 0,
         f"房型 {pick.get('room_type_name') if pick else types} 总价 "
         f"{pick.get('total_amount') if pick else '-'}")
    if pick is None:
        raise SystemExit("A1 失败，无法继续链路")

    rt_id = pick["room_type_id"]
    rate_plan_id = pick["rate_plan_id"]
    total = Decimal(str(pick["total_amount"]))
    nightly = Decimal(str(pick["nightly_details"][0]["price"]))
    expected_dep = q2(total * Decimal("30") / Decimal("100"))

    payload = {"full_name": "链路测试客", "phone": "13911112222",
               "room_type_id": rt_id, "rate_plan_id": rate_plan_id,
               "check_in": CHECK_IN, "check_out": CHECK_OUT,
               "rooms": 1, "adults": 1, "children": 0}
    code, body = api(anon, "POST", "/api/v1/reservations", payload, idem="e2e-chain-001")
    booking_no = body.get("booking_no")
    token = body.get("access_token") or ""
    step("A2 公开预订创建 PENDING（一次性令牌）",
         code == 201 and body.get("status") == "PENDING" and token
         and Decimal(str(body.get("total_amount") or 0)) == total,
         f"{booking_no} 总价 {body.get('total_amount')}")

    # §6.4 pending cap=2：同手机号第 2 单放行、第 3 单 409（rate_limit phone 3/600s 内）
    code, body_cap2 = api(anon, "POST", "/api/v1/reservations",
                          dict(payload, full_name="链路测试客第二单"),
                          idem="e2e-chain-003")
    bn1b = body_cap2.get("booking_no")
    cap2_ok = code == 201 and body_cap2.get("status") == "PENDING" and bn1b
    code, body_cap3 = api(anon, "POST", "/api/v1/reservations",
                          dict(payload, full_name="链路测试客第三单"),
                          idem="e2e-chain-004")
    step("A3 未支付 PENDING 上限 2（第 3 单 409 拒绝）",
         cap2_ok and code == 409 and body_cap3.get("error", {}).get("code") == "BUSINESS_CONFLICT",
         "第 2 单 %s 放行；第 3 单 HTTP %d %s"
         % (bn1b, code, body_cap3.get("error", {}).get("message", "")[:60]))

    payload2 = dict(payload, phone="13911113333", full_name="链路预付客")
    code, body2 = api(anon, "POST", "/api/v1/reservations", payload2, idem="e2e-chain-002")
    bn2, token2 = body2.get("booking_no"), body2.get("access_token") or ""
    step("A4 第二单创建（预付链，不同手机号）",
         code == 201 and body2.get("status") == "PENDING" and bn2 and token2,
         str(bn2))

    code, intent = api(anon, "POST", "/api/v1/payments/intents",
                       {"booking_no": bn2, "token": token2})
    sample = (intent.get("mock_callback_example") or {}) if isinstance(intent, dict) else {}
    step("A5 支付意图创建（dev 模拟回调样例）",
         code == 201 and sample.get("url") and sample.get("signature")
         and str(intent.get("provider_ref", "")).startswith("MOCK-"),
         f"provider_ref={intent.get('provider_ref')} 金额 {intent.get('amount')}")

    raw = json.dumps(sample["payload"], ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    r = anon.post(BASE + sample["url"], data=raw, timeout=15,
                  headers={"Content-Type": "application/json",
                           "X-Signature": sample["signature"]})
    try:
        cb = r.json()
    except Exception:
        cb = {}
    step("A6 模拟回调验签 → 订单 CONFIRMED",
         r.status_code == 200 and cb.get("ok") is True
         and cb.get("reservation_status") == "CONFIRMED"
         and cb.get("reservation_confirmed") is True,
         f"HTTP {r.status_code} {str(cb)[:100]}")

    r = anon.post(BASE + sample["url"], data=raw, timeout=15,
                  headers={"Content-Type": "application/json",
                           "X-Signature": sample["signature"]})
    try:
        cb2 = r.json()
    except Exception:
        cb2 = {}
    step("A7 回调幂等重放（duplicate=true 不重复入账）",
         r.status_code == 200 and cb2.get("duplicate") is True
         and cb2.get("reservation_status") == "CONFIRMED",
         f"duplicate={cb2.get('duplicate')}")

    code, detail = api(anon, "GET", f"/api/v1/reservations/{bn2}",
                       headers={"X-Booking-Token": token2})
    whitelist = {"booking_no", "status", "guest_name", "check_in", "check_out",
                 "rooms", "total_amount", "currency", "payments"}
    step("A8 令牌查询订单（字段白名单 + 姓名脱敏）",
         code == 200 and detail.get("status") == "CONFIRMED"
         and set(detail.keys()) == whitelist
         and "*" in str(detail.get("guest_name")),
         str({k: detail.get(k) for k in ("booking_no", "status", "guest_name")}))

    assert_envelope("A9 错误封套 {error:{code,message}}（错误令牌 404）",
                    *api(anon, "GET", f"/api/v1/reservations/{bn2}",
                         headers={"X-Booking-Token": "wrong-token"}),
                    404, "RESOURCE_NOT_FOUND")

    # ================================================================ B 前台链
    front = login("test_front")
    code, d = api(front, "GET", f"/api/v1/front-desk/reservations?q={booking_no}")
    rows = d.get("data", [])
    row = next((x for x in rows if x.get("booking_no") == booking_no), None)
    step("B1 订单查询列表可见新订单并携带 stay_status/reservation_status 新字段",
         code == 200 and row is not None
         and row.get("reservation_status") == "PENDING"
         and "stay_status" in row and row.get("stay_status") is None
         and "assigned_room" in row,
         f"rows={len(rows)} reservation_status={row.get('reservation_status') if row else '-'} "
         f"stay_status={row.get('stay_status') if row else '-'}")

    code, d = api(front, "GET", f"/api/v1/front-desk/reservations/{booking_no}")
    inner = d.get("data", d)
    lines = inner.get("rooms") or []
    rr = lines[0] if lines else {}
    rr_id = rr.get("reservation_room_id") or rr.get("id")
    step("B2 订单详情含预订房间明细",
         code == 200 and rr_id is not None
         and str((rr.get("amount") if rr else "")) == str(total),
         f"reservation_room={rr_id} amount={rr.get('amount') if rr else '-'}")

    code, d = api(front, "POST", "/api/v1/billing/shifts/open", {"opening_cash": "0.00"})
    shift_id = (d.get("data") or {}).get("id")
    step("B3 前台开立本人收银班次（备用金 0）",
         code in (200, 201) and shift_id, f"shift={shift_id}")

    # 押金 30% 服务端强制（WB-030/DEC-DEP-01）：调用方金额被忽略
    code, d = api(front, "POST", f"/api/v1/front-desk/reservations/{booking_no}/deposits",
                  {"amount": "1.00", "method": "CASH", "reference": ""},
                  idem="e2e-dep-001")
    dep_pay_id = (d.get("data") or {}).get("payment_id")
    dep_amount = (d.get("data") or {}).get("amount")
    step("B4 押金 30% 服务端强制（调用方 1.00 被忽略，PENDING 收款即确认）",
         code == 201 and dep_pay_id
         and dep_amount == str(expected_dep)
         and (d.get("data") or {}).get("payment_kind") == "DEPOSIT"
         and dep_amount != "1.00",
         f"请求 1.00 → 服务端计得 {dep_amount}（应为 {expected_dep}）")

    code, d = api(front, "GET", f"/api/v1/front-desk/reservations/{booking_no}")
    step("B5 押金收款后订单自动确认",
         code == 200 and (d.get("data") or {}).get("status") == "CONFIRMED",
         (d.get("data") or {}).get("status"))

    code, d = api(front, "POST", f"/api/v1/front-desk/reservations/{booking_no}/deposits",
                  {"amount": "1.00", "method": "CASH"}, idem="e2e-dep-001")
    step("B6 押金同幂等键重放 → 200 created=false",
         code == 200 and (d.get("data") or {}).get("amount") == str(expected_dep)
         and d.get("created") is False,
         f"HTTP {code} created={d.get('created')}")

    code, rooms = api(front, "GET",
                      f"/api/v1/front-desk/rooms/assignable?reservation_room_id={rr_id}")
    room_list = rooms.get("data", [])
    room_id = room_list[0].get("id") if room_list else None
    step("B7 可排房查询返回净房", code == 200 and room_id is not None,
         f"{len(room_list)} 间，首间 {room_list[0].get('number') if room_list else '-'}")

    code, d = api(front, "POST",
                  f"/api/v1/front-desk/reservation-rooms/{rr_id}/assign",
                  {"room_id": room_id})
    step("B8 排房成功",
         code in (200, 201) and (d.get("data") or {}).get("assigned_room", {}).get("id") == room_id,
         f"room={room_id}")

    code, d = api(front, "POST",
                  f"/api/v1/front-desk/reservation-rooms/{rr_id}/check-in", {})
    stay_id = (d.get("data") or {}).get("stay_id")
    step("B9 办理入住 → Stay(IN_HOUSE)",
         code == 201 and stay_id and (d.get("data") or {}).get("status") == "IN_HOUSE",
         f"stay={stay_id}")

    code, d = api(front, "POST", f"/api/v1/front-desk/reservations/{booking_no}/payments",
                  {"amount": "10.00", "method": "CASH"}, idem="e2e-pay-001")
    pay_id = (d.get("data") or {}).get("payment_id")
    step("B10 在住登记现金收款 10.00（挂本人班次）",
         code == 201 and pay_id and (d.get("data") or {}).get("amount") == "10.00",
         f"payment={pay_id}")

    # Walk-in 标准端点（A1 契约）：服务端 30% 押金、created 标志、幂等重放
    code, d = api(front, "POST", "/api/v1/front-desk/walk-ins",
                  {"full_name": "链路散客甲", "phone": "13911117777",
                   "room_type_id": rt_id, "rate_plan_id": rate_plan_id,
                   "nights": 1, "adults": 1, "children": 0,
                   "deposit_amount": "0.01"},
                  idem="e2e-walkin-001")
    walkin_no = (d.get("data") or {}).get("booking_no")
    walkin_total = nightly  # 1 晚散客：总价 = 当晚房价
    walkin_dep = q2(nightly * Decimal("30") / Decimal("100"))
    step("B11 walk-ins 标准端点 201（押金=总额×30%，服务端计得）",
         code == 201 and walkin_no and (d.get("data") or {}).get("created") is True
         and (d.get("data") or {}).get("stay_id")
         and (d.get("data") or {}).get("deposit_amount") == str(walkin_dep)
         and (d.get("data") or {}).get("deposit_amount") != "0.01",
         f"{walkin_no} 请求押金 0.01 → 服务端 {walkin_dep}（{nightly}×30%）")

    code, d = api(front, "POST", "/api/v1/front-desk/walk-ins",
                  {"full_name": "链路散客甲", "phone": "13911117777",
                   "room_type_id": rt_id, "rate_plan_id": rate_plan_id,
                   "nights": 1, "adults": 1, "children": 0,
                   "deposit_amount": "0.01"},
                  idem="e2e-walkin-001")
    step("B12 walk-ins 同幂等键重放 → 200 created=false（同一 Stay）",
         code == 200 and (d.get("data") or {}).get("created") is False
         and (d.get("data") or {}).get("booking_no") == walkin_no,
         f"HTTP {code} created={(d.get('data') or {}).get('created')}")

    # 旧端点 /walk-in 已按 DEC-WI-01 收敛退役（e56164d + 8ebf918）：
    # 恒返 410 Gone + error.code=GONE，不读请求体、不做业务写入。
    # 本步改为验证「旧入口确已退役」，而非断言 201 创建成功。
    # 旧端点在读请求体之前即返回 410，故请求体只需最小可用载荷
    # （此处保留空 dict，进一步证明「不读请求体、不做业务写入」）。
    code, d = api(front, "POST", "/api/v1/front-desk/walk-in", {},
                  idem="e2e-walkin-002")
    gone_code = (d.get("error") or {}).get("code")
    step("B13 旧 /walk-in 端点已按 DEC-WI-01 退役（410 Gone，无业务写入）",
         code == 410 and gone_code == "GONE",
         f"HTTP {code} code={gone_code}")

    # BillingInputError → 400 分流（RSV-10）：非法支付方式
    assert_envelope("B14 BillingInputError 分流 400（押金方式非法）",
                    *api(front, "POST",
                         f"/api/v1/front-desk/reservations/{bn2}/deposits",
                         {"amount": "1.00", "method": "WECHAT"}, idem="e2e-dep-bad"),
                    400, "VALIDATION_ERROR", "押金方式")

    # ================================================================ C 夜审链
    duty = login("test_duty")
    inhouse_expected = 2  # booking1 + walk-in 甲（旧端点已退役，仅两单）
    expected_charge_total = q2(nightly + walkin_total)
    code, d = api(duty, "GET", f"/api/v1/night-audit/preview?date={CHECK_IN}")
    inner = d.get("data", d)
    step("C1 夜审预览无阻断（在住 2 单，待过租 2 笔）",
         code == 200 and inner.get("blocker_count") == 0
         and inner.get("stay_count") == inhouse_expected
         and inner.get("charge_count") == inhouse_expected
         and inner.get("charge_total") == str(expected_charge_total),
         f"阻断 {inner.get('blocker_count')} 在住 {inner.get('stay_count')} "
         f"待过租 {inner.get('charge_count')} 笔 {inner.get('charge_total')}")

    # WB-080：夜审只允许当前营业日（未来日期拒绝）
    code, d = api(duty, "GET", f"/api/v1/night-audit/preview?date={tomorrow.isoformat()}")
    fut_preview_ok = (code == 409
                      and d.get("error", {}).get("code") == "BUSINESS_CONFLICT"
                      and "只允许审计当前营业日" in d.get("error", {}).get("message", ""))
    code, d = api(duty, "POST", "/api/v1/night-audit/run", {"date": tomorrow.isoformat()})
    step("C2 wb 守卫：非当前营业日（明日）预览/执行一律 409",
         fut_preview_ok and code == 409
         and "只允许审计当前营业日" in d.get("error", {}).get("message", ""),
         f"preview 409 ✓ / run HTTP {code} "
         f"{d.get('error', {}).get('message', '')[:60]}")

    code, d = api(duty, "POST", "/api/v1/night-audit/run", {"date": CHECK_IN})
    run_data = d.get("data") or {}
    run_id = run_data.get("run_id")
    summary = run_data.get("summary") or {}
    step("C3 执行夜审成功并关账（过租 2 笔）",
         code == 201 and run_data.get("status") == "SUCCESS"
         and summary.get("charge_count") == inhouse_expected
         and summary.get("charge_total") == str(expected_charge_total),
         f"run={run_id} charge={summary.get('charge_count')} "
         f"total={summary.get('charge_total')}")

    code, d = api(duty, "GET", "/api/v1/night-audit/business-dates/current")
    current_bd = d.get("data") or {}
    code2, d2 = api(duty, "GET", "/api/v1/night-audit/business-dates?status=OPEN")
    open_dates = [x for x in (d2.get("data") or [])
                  if x.get("business_date") == CHECK_IN]
    code3, d3 = api(duty, "POST", "/api/v1/night-audit/run", {"date": CHECK_IN})
    step("C4 营业日已关闭（单开不变量 + 关账后 run 幂等重放 200）",
         code == 200 and current_bd.get("status") == "CLOSED"
         and current_bd.get("exists") is True
         and code2 == 200 and not open_dates
         and code3 == 200 and (d3.get("data") or {}).get("created") is False,
         f"current={current_bd.get('status')} OPEN 口径 {len(open_dates)} 条 "
         f"重放 HTTP {code3} created={(d3.get('data') or {}).get('created')}")

    # WB-079 关账守卫：CLOSED 营业日禁止新增账务凭证（员工入账一律拦截）
    assert_envelope("C5 关账守卫：CLOSED 营业日收款拒绝（WB-079）",
                    *api(front, "POST",
                         f"/api/v1/front-desk/reservations/{booking_no}/payments",
                         {"amount": "5.00", "method": "CASH"}, idem="e2e-pay-closed"),
                    409, "BUSINESS_CONFLICT", "已关账")

    # 营业日重开授权链：duty 申请 → SoD 自批拒绝 → owner 批准 REOPENED
    code, d = api(duty, "GET", "/api/v1/night-audit/business-dates?status=CLOSED")
    bd_row = next((x for x in (d.get("data") or [])
                   if x.get("business_date") == CHECK_IN), None)
    bd_id = bd_row.get("id") if bd_row else None
    code, d = api(duty, "POST",
                  f"/api/v1/night-audit/business-dates/{bd_id}/reopen-request",
                  {"reason": "链路测试：夜审后退房需补收结算（G14-09 e2e）"})
    approval_id = (d.get("data") or {}).get("approval_id")
    step("C6 营业日重开申请 201（duty 发起）",
         code == 201 and approval_id, f"approval={approval_id}")

    code, d = api(duty, "POST",
                  f"/api/v1/night-audit/business-dates/{bd_id}/approvals/{approval_id}/approve",
                  {})
    step("C7 重开申请 SoD：申请人自批被拒（Q-05）",
         code == 409 and "申请人不能审批自己的申请" in d.get("error", {}).get("message", ""),
         f"HTTP {code} {d.get('error', {}).get('message', '')[:60]}")

    owner = login("test_owner")
    code, d = api(owner, "POST",
                  f"/api/v1/night-audit/business-dates/{bd_id}/approvals/{approval_id}/approve",
                  {})
    step("C8 owner 批准 → 营业日 REOPENED",
         code == 200 and (d.get("data") or {}).get("status") == "REOPENED",
         str(d.get("data", {}).get("status")))

    # ================================================================ D 退房链
    code, d = api(front, "GET", f"/api/v1/front-desk/folios/{booking_no}")
    folio = d.get("data", d)
    entries = folio.get("entries") or []
    room_charges = [e for e in entries if e.get("entry_type") == "ROOM_CHARGE"]
    balance_d1 = (folio.get("summary") or {}).get("balance")
    expected_balance = q2(nightly - expected_dep - Decimal("10.00"))
    step("D1 夜审后账本已入当晚房费（1 笔，无整单双记）",
         code == 200 and len(room_charges) == 1
         and room_charges[0].get("reference", "").startswith("night-audit:")
         and Decimal(str(room_charges[0]["amount"])) == nightly
         and Decimal(str(balance_d1)) == expected_balance,
         f"房费 {room_charges[0]['amount'] if room_charges else '-'} / "
         f"余额 {balance_d1}（应为 {expected_balance}）")

    code, d = api(front, "POST", f"/api/v1/front-desk/stays/{stay_id}/check-out", {})
    err_msg = d.get("error", {}).get("message", "") if isinstance(d, dict) else ""
    m_amt = re.search(r"应收余额 ([0-9.]+)", err_msg)
    owe = m_amt.group(1) if m_amt else ""
    step("D2 未结清退房明确拒绝（409 + 应收余额金额可解析）",
         code == 409 and d.get("error", {}).get("code") == "BUSINESS_CONFLICT"
         and owe != "" and Decimal(owe) == expected_balance,
         f"HTTP {code} {err_msg[:80]}")

    code, d = api(front, "POST", f"/api/v1/front-desk/reservations/{booking_no}/payments",
                  {"amount": owe, "method": "CARD"}, idem="e2e-pay-002")
    step("D2a 按守卫提示补收差额",
         code == 201 and (d.get("data") or {}).get("amount") == owe,
         f"补收 {owe}")

    code, d = api(front, "POST", f"/api/v1/front-desk/stays/{stay_id}/check-out", {})
    step("D2b 退房成功（FIRST_NIGHT 罚金两讫后放行）",
         code in (200, 201) and (d.get("data") or {}).get("status") == "CHECKED_OUT",
         str((d.get("data") or {}).get("status")))

    code, d = api(front, "GET", f"/api/v1/front-desk/folios/{booking_no}")
    folio = d.get("data", d)
    summary_d3 = folio.get("summary") or {}
    entries = folio.get("entries") or []
    balance = summary_d3.get("balance")
    step("D3 退房后账本两讫（余额 0 且账单 CLOSED）",
         code == 200 and Decimal(str(balance)) == Decimal("0.00")
         and summary_d3.get("status") == "CLOSED",
         f"余额 {balance} 状态 {summary_d3.get('status')}")

    rc_total = sum((Decimal(str(e["amount"])) for e in entries
                    if e.get("entry_type") == "ROOM_CHARGE"), Decimal("0.00"))
    reversals = [e for e in entries
                 if e.get("entry_type") == "ADJUSTMENT"
                 and str(e.get("reference", "")).startswith("checkout-adjust:")]
    penalties = [e for e in entries
                 if e.get("entry_type") == "ADJUSTMENT"
                 and str(e.get("reference", "")).startswith("ed-penalty:")]
    step("D3a 房费账实相符（夜审 1 晚入账，退房净额冲回 + FIRST_NIGHT 罚金，无双记）",
         rc_total == nightly and len(reversals) == 1
         and Decimal(str(reversals[0]["amount"])) == -nightly
         and len(penalties) == 1
         and Decimal(str(penalties[0]["amount"])) == nightly,
         f"已入房费 {rc_total}（合同 {total}，实际 0 晚 + 罚金 {nightly}）")

    hk = login("test_housekeeping")
    code, d = api(hk, "GET", "/api/v1/housekeeping/rooms")
    hrooms = d.get("data", [])
    target = next((x for x in hrooms if x.get("id") == room_id), None)
    step("D4 清洁工作台可见退房脏房（DIRTY）",
         code == 200 and target is not None
         and target.get("housekeeping_status") == "DIRTY"
         and target.get("occupancy_status") == "VACANT",
         str({k: (target or {}).get(k) for k in ("number", "housekeeping_status")}))

    code, d = api(hk, "POST", f"/api/v1/housekeeping/rooms/{room_id}/status",
                  {"status": "CLEANING"})
    step("D5 客房开始清洁 → CLEANING", code == 200, f"HTTP {code}")

    code, d = api(hk, "POST", f"/api/v1/housekeeping/rooms/{room_id}/status",
                  {"status": "INSPECTING"})
    step("D5b 清洁完成待检查 → INSPECTING", code == 200, f"HTTP {code}")

    code, d = api(hk, "POST", f"/api/v1/housekeeping/rooms/{room_id}/status",
                  {"status": "CLEAN"})
    step("D5c 服务员净房放行必须被权限层拒绝（403）",
         code == 403 and d.get("error", {}).get("code") == "PERMISSION_DENIED",
         f"HTTP {code} {d.get('error', {}).get('message', '')[:60]}")

    code, d = api(duty, "POST", f"/api/v1/housekeeping/rooms/{room_id}/status",
                  {"status": "CLEAN"})
    step("D6 值班经理检查放行 → CLEAN（净房）",
         code == 200 and (d.get("data") or {}).get("housekeeping_status") == "CLEAN",
         f"HTTP {code}")

    # ================================================================ E 退款三权分立
    # 幂等键头必带：无 Idempotency-Key 的退款申请被服务层拒绝（该端点按
    # RSV-05 口径将服务层 ValueError 统一映射 409 BUSINESS_CONFLICT）
    assert_envelope("E1 退款申请缺 Idempotency-Key 头 → 拒绝",
                    *api(front, "POST",
                         f"/api/v1/front-desk/payments/{dep_pay_id}/refunds",
                         {"amount": "50.00", "reason": "链路测试：部分退款"}),
                    409, "BUSINESS_CONFLICT", "幂等键")

    code, d = api(front, "POST",
                  f"/api/v1/front-desk/payments/{dep_pay_id}/refunds",
                  {"amount": "50.00", "reason": "链路测试：部分退款"},
                  idem="e2e-ref-001")
    refund_id = (d.get("data") or {}).get("refund_id")
    step("E1b 前台退款申请 201（PENDING，押金流水）",
         code == 201 and refund_id
         and (d.get("data") or {}).get("status") == "PENDING"
         and Decimal(str((d.get("data") or {}).get("amount") or 0)) == Decimal("50.00"),
         f"refund={refund_id}")

    code, d = api(front, "POST", f"/api/v1/front-desk/refunds/{refund_id}/approve",
                  {})
    step("E1c 申请人自批被权限层拒绝（前台无 billing.refund_approve）",
         code == 403 and d.get("error", {}).get("code") == "PERMISSION_DENIED",
         f"HTTP {code}")

    code, d = api(duty, "POST", f"/api/v1/billing/refunds/{refund_id}/approve",
                  {"comment": "链路测试审批"})
    step("E2 值班经理审批 → APPROVED（待独立执行）",
         code == 200 and (d.get("data") or {}).get("status") == "APPROVED"
         and (d.get("data") or {}).get("approval_record_id"),
         f"refund={refund_id} status={(d.get('data') or {}).get('status')}")

    fin = login("test_finance")
    code, d = api(fin, "POST", "/api/v1/billing/shifts/open", {"opening_cash": "0.00"})
    fin_shift = (d.get("data") or {}).get("id")
    code, d = api(fin, "POST", f"/api/v1/billing/shifts/{fin_shift}/movements",
                  {"direction": "IN", "amount": "100.00",
                   "reason": "退款备用金补入（链路测试）"}, idem="e2e-mv-001")
    step("E3a 财务开班 + 现金补入 100.00",
         code == 201 and fin_shift and (d.get("data") or {}).get("amount") == "100.00",
         f"shift={fin_shift}")

    code, d = api(fin, "POST", f"/api/v1/billing/refunds/{refund_id}/execute",
                  {"confirm_refund_paid": True,
                   "execution_reference": "E2E-REFUND-001",
                   "idempotency_key": "e2e-refund-exec-001"})
    step("E3 财务执行退款 → SUCCEEDED（REFUND 凭证入账）",
         code == 201 and (d.get("data") or {}).get("status") == "SUCCEEDED"
         and (d.get("data") or {}).get("executed_by_id"),
         f"refund={refund_id} shift={(d.get('data') or {}).get('execution_shift_id')}")

    code, d = api(duty, "POST", f"/api/v1/billing/refunds/{refund_id}/execute",
                  {"confirm_refund_paid": True,
                   "execution_reference": "E2E-REFUND-002",
                   "idempotency_key": "e2e-refund-exec-002"})
    step("E4 非财务执行人（duty 无 billing.refund_execute）必须 403",
         code == 403 and d.get("error", {}).get("code") == "PERMISSION_DENIED",
         f"HTTP {code} {d.get('error', {}).get('message', '')[:60]}")

    # ================================================================ F 互通断言
    code, d = api(front, "POST", "/api/v1/notifications/refresh", {})
    refresh_summary = d.get("data") or {}
    code, d = api(front, "GET", "/api/v1/notifications")
    notif_items = d.get("data") or []
    matched = [n for n in notif_items
               if bn1b in str(n.get("title", "")) or bn2 in str(n.get("title", ""))]
    step("F1 通知中心派生可验证业务工作项（待确认/今日预抵含预订号）",
         code == 200 and isinstance(notif_items, list) and len(notif_items) > 0
         and matched and refresh_summary.get("active", 0) > 0
         and all(n.get("category") for n in matched),
         f"刷新 active={refresh_summary.get('active')} created="
         f"{refresh_summary.get('created')}；命中 {len(matched)} 条"
         f"（如 {matched[0].get('title') if matched else '-'}）")

    code, d = api(owner, "GET",
                  "/api/v1/audit/events?action=night_audit.run&page_size=5")
    audit_items = d.get("data") or []
    step("F2 审计中心可查询夜审审计（action=night_audit.run）",
         code == 200 and len(audit_items) >= 1
         and audit_items[0].get("action") == "night_audit.run",
         f"{len(audit_items)} 条，最近 entity={audit_items[0].get('entity_type') if audit_items else '-'}")

    code, d = api(owner, "GET",
                  "/api/v1/audit/events?action=housekeeping.task_created&page_size=5")
    task_events = d.get("data") or []
    step("F2b 退房自动生成清洁任务留痕（WB-055）",
         code == 200 and len(task_events) >= 1,
         f"{len(task_events)} 条 housekeeping.task_created")

    code, d = api(owner, "GET",
                  f"/api/v1/reports/occupancy?from={CHECK_IN}&to={CHECK_IN}")
    occ = d.get("data") or {}
    series = occ.get("series") or []
    today_row = next((x for x in series if x.get("date") == CHECK_IN), None)
    step("F3 经营报表可取数（当日已售房晚 ≥1）",
         code == 200 and today_row is not None
         and int(today_row.get("sold_room_nights") or 0) >= 1,
         f"today sold={today_row.get('sold_room_nights') if today_row else '-'} "
         f"occupancy={today_row.get('occupancy_pct') if today_row else '-'}")

    code, d = api(anon, "GET", f"/api/v1/reservations/{booking_no}",
                  headers={"X-Booking-Token": token})
    payments_guest = d.get("payments") or []
    dep_row = next((p for p in payments_guest
                    if Decimal(str(p.get("amount") or 0)) == expected_dep), None)
    step("F4 顾客侧订单状态同步（CHECKED_OUT + 押金 PARTIALLY_REFUNDED）",
         code == 200 and d.get("status") == "CHECKED_OUT"
         and dep_row is not None and dep_row.get("status") == "PARTIALLY_REFUNDED",
         f"status={d.get('status')} 押金状态={dep_row.get('status') if dep_row else '-'}")

    # 账本只追加：优先 DB 层 append-only 触发器（UPDATE/DELETE 必须被拒）；
    # 本机 MySQL app 用户无 SUPER（1419）时降级为 API 只追加契约面断言
    # （账本无 POST/PUT/DELETE 写路由，冲销只能走等额反向凭证服务入口）。
    conn = _db_cursor()
    append_only_ok = False
    append_only_detail = ""
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT trigger_name FROM information_schema.triggers "
                "WHERE event_object_table='billing_ledgerentry' "
                "AND trigger_schema=DATABASE()")
            triggers = {row[0] for row in cursor.fetchall()}
            cursor.execute("SELECT id FROM billing_ledgerentry ORDER BY id LIMIT 1")
            row = cursor.fetchone()
            entry_id = row[0] if row else None
            if {"trg_billing_ledgerentry_no_update",
                "trg_billing_ledgerentry_no_delete"} <= triggers and entry_id:
                try:
                    cursor.execute(
                        "UPDATE billing_ledgerentry SET description='tampered' "
                        "WHERE id=%s", (entry_id,))
                    append_only_detail = "UPDATE 未被触发器拦截"
                except Exception:
                    try:
                        cursor.execute(
                            "DELETE FROM billing_ledgerentry WHERE id=%s", (entry_id,))
                        append_only_detail = "DELETE 未被触发器拦截"
                    except Exception:
                        append_only_ok = True
                        append_only_detail = (
                            f"DB 层强断言：UPDATE/DELETE 均被 append-only 触发器"
                            f"拒绝（entry {entry_id}）")
            else:
                append_only_detail = "触发器未安装（%s）" % (
                    TRIGGER_INSTALL_NOTE[:80] or "无触发器")
        conn.rollback()
    finally:
        conn.close()
    if not append_only_ok:
        code_put, d_put = api(front, "POST", "/api/v1/billing/ledger", {"amount": "1.00"})
        code_del, d_del = api(front, "DELETE", "/api/v1/billing/ledger")
        # 该端点 require_GET 的 405 为空体（非 JSON 封套），以状态码为准：
        # POST/DELETE 均无写路由，账本只经 BillingService.post_entry 追加。
        api_surface_ok = code_put == 405 and code_del == 405
        append_only_ok = api_surface_ok
        append_only_detail = (
            "API 面断言（触发器权限边界 1419，CI MySQL 以 root 安装）："
            f"POST/DELETE 账本路由 HTTP {code_put}/{code_del} 均 405（无写路由），"
            "写路径仅 BillingService.post_entry 追加入口")
    step("F5 LedgerEntry 只追加（DB 触发器拒改删 / API 面无写路由）",
         append_only_ok, append_only_detail)

    # LifecycleOperationError → 409 分流（RSV-10）：退房后订单不可再收押金
    assert_envelope("F6 业务状态冲突分流 409（退房后订单收押金被拒）",
                    *api(front, "POST",
                         f"/api/v1/front-desk/reservations/{booking_no}/deposits",
                         {"amount": "1.00", "method": "CASH"}, idem="e2e-dep-after-out"),
                    409, "BUSINESS_CONFLICT")

    # ---------------------------------------------------------------- 汇总
    fails = [r_ for r_ in RESULTS if not r_[1]]
    print("\n===== 串测汇总: %d 项 / 失败 %d 项 =====" % (len(RESULTS), len(fails)))
    for name, ok, detail in fails:
        print("  FAIL:", name, "|", detail)
    print("run_finished_at:", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    return 0 if not fails else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        try:
            sys.stdout = sys.__stdout__
            _EVIDENCE_FILE.flush()
            _EVIDENCE_FILE.close()
        except Exception:
            pass
