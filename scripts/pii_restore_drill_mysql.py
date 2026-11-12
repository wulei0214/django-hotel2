# -*- coding: utf-8 -*-
"""WB-124：手机号 PII 迁移——**MySQL 生产型**隔离环境备份恢复演练（第 32 轮）。

SQLite 演练（pii_restore_drill.py）证明迁移数据恢复流程；本脚本在真实
MySQL 8 上用 mysqldump/mysql 客户端完成同一链路，证明生产型数据库的
备份恢复与密钥恢复能力：

  Phase 0  重置演练库 test_hotel（DROP/CREATE，应用账号仅授权
           hotel/test_hotel 两个库——本脚本硬性校验目标必须是
           test_hotel，绝不触碰 dev `hotel` / 生产库）
  Phase 1  初始化：其他应用全部迁移至最新，guests 停在 0006（迁移前
           状态）；按 0006 时刻 DDL 原始 SQL 写入明文组合数据
  Phase 2  迁移前备份：mysqldump --single-transaction 全库备份 +
           行数核对（备份成功 ≠ 备份可用，Phase 4 验证可恢复性）
  Phase 3  前向迁移：应用 guests 最新（0007 回填+清明文），核对数量、
           明文清除、解密、HMAC、业务读取入口
  Phase 4  灾难恢复：DROP DATABASE → mysql < 备份恢复 → 核对恢复后
           行数、迁移状态（guests 停在 0006）与原始明文完整
  Phase 5  恢复后重放迁移：二次核对解密 / HMAC / 明文清除

每步输出 [PASS]/[FAIL]；任何失败以非零退出码结束。
运行方式（PowerShell，env 注入；DJANGO_DB_NAME 必须为 test_hotel）：
  $env:DJANGO_ENV='test'; $env:DJANGO_DB_NAME='test_hotel';
  python scripts/pii_restore_drill_mysql.py
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hotel.settings")

import django  # noqa: E402

django.setup()

from django.apps import apps  # noqa: E402
from django.conf import settings  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402

from apps.core.models import Property  # noqa: E402
from apps.guests import services as guest_services  # noqa: E402
from apps.guests.models import Guest, GuestPhone  # noqa: E402

FAILURES = []
STEP = [0]
DB_CONFIG = settings.DATABASES["default"]
DB_NAME = DB_CONFIG["NAME"]
MYSQL = "mysql"
MYSQLDUMP = "mysqldump"
COMMON_ARGS = [
    f"--host={DB_CONFIG['HOST'] or 'localhost'}",
    f"--port={DB_CONFIG['PORT'] or '3306'}",
    f"--user={DB_CONFIG['USER']}",
    f"--password={DB_CONFIG['PASSWORD']}",
    "--default-character-set=utf8mb4",
]
BACKUP_FILE = os.path.join("logs", "pii_drill_mysql_backup.sql")


def step(title):
    STEP[0] += 1
    print(f"--- Phase {STEP[0]}: {title}", flush=True)


def check(label, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" | {detail}" if detail else ""), flush=True)
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def sql_cli(database, sql=None, input_file=None):
    args = [MYSQL, *COMMON_ARGS]
    if sql is not None:
        args += ["--execute", sql]
    args.append(database)
    if input_file:
        with open(input_file, "rb") as fh:
            return subprocess.run(args, stdin=fh, capture_output=True, text=True)
    return subprocess.run(args, capture_output=True, text=True)


def verify_backfill(expected, gp_expected, tag, prop):
    """核对回填/清明文结果：数量、明文清除、解密、HMAC、业务读取。

    行数只核对演练物业（其他应用迁移可能播种 DEFAULT 物业的 Guest）。
    """
    rows_ok = (Guest.objects.filter(property=prop).count() == len(expected)
               and GuestPhone.objects.filter(
                   guest__property=prop).count() == len(gp_expected))
    check(f"{tag}：迁移后行数一致", rows_ok,
          f"Guest={Guest.objects.filter(property=prop).count()} "
          f"GuestPhone={GuestPhone.objects.filter(guest__property=prop).count()}")
    for model, mapping in ((Guest, expected), (GuestPhone, gp_expected)):
        for pk, plain in mapping.items():
            row = model.objects.get(pk=pk)
            if plain is None:
                ok = (not (row.phone_ciphertext or "")
                      and not (row.phone_hash or ""))
                check(f"{tag}：无号码记录不伪造密文 model={model.__name__}#{pk}",
                      ok)
            else:
                ok = (row.phone_ciphertext and row.phone_hash
                      and guest_services.decrypt_phone(
                          row.phone_ciphertext) == plain
                      and row.phone_hash == guest_services.phone_hash(plain))
                check(f"{tag}：密文可解密且 HMAC 一致 "
                      f"model={model.__name__}#{pk}", ok, plain)
            if row.phone is not None or row.phone_normalized is not None:
                check(f"{tag}：明文已清除 model={model.__name__}#{pk}", False,
                      f"phone={row.phone!r} normalized={row.phone_normalized!r}")
    biz_ok = all(
        guest_services.guest_phone_plain(Guest.objects.get(pk=pk))
        == (plain if plain is not None else "")
        for pk, plain in expected.items())
    check(f"{tag}：业务读取 guest_phone_plain 正常", biz_ok)


def guests_migration_state():
    with connection.cursor() as cur:
        cur.execute(
            "SELECT app, MAX(name) FROM django_migrations WHERE app='guests' "
            "GROUP BY app")
        row = cur.fetchone()
    return row[1] if row else None


def main():
    if DB_NAME != "test_hotel":
        print(f"REFUSED：演练目标必须是 test_hotel（当前 DJANGO_DB_NAME="
              f"{DB_NAME!r}）。本脚本不触碰 dev `hotel` / 生产库。")
        sys.exit(2)
    os.makedirs("logs", exist_ok=True)

    # ---------- Phase 0: 重置演练库 ----------
    step(f"重置演练库 {DB_NAME}（DROP/CREATE，真实 MySQL 8）")
    r = sql_cli("", sql=f"DROP DATABASE IF EXISTS {DB_NAME}; "
                        f"CREATE DATABASE {DB_NAME} CHARACTER SET utf8mb4 "
                        f"COLLATE utf8mb4_unicode_ci;")
    check("演练库重建成功", r.returncode == 0, r.stderr.strip()[-200:])

    # ---------- Phase 1: 初始化迁移前状态（guests 0006） ----------
    step("初始化：其他应用迁至最新，guests 停在 0006（迁移前状态）")
    call_command("migrate", "guests", "0006", verbosity=0, interactive=False)
    for app_config in apps.get_app_configs():
        label = app_config.label
        if label == "guests" or not app_config.models_module:
            continue
        try:
            call_command("migrate", label, verbosity=0, interactive=False)
        except Exception as exc:
            # 应用可能没有自己的迁移或已被依赖连带迁移，忽略无害错误
            print(f"  (migrate {label}: {type(exc).__name__}: {exc})")
    check("guests 迁移状态停在 0006",
          (guests_migration_state() or "").startswith("0006"),
          guests_migration_state())

    prop, _ = Property.objects.get_or_create(
        code="DRILLM", defaults={"name": "WB-124 MySQL 恢复演练物业"})
    # 0006 时刻 phone 列 NOT NULL：按 0006 时刻 DDL 原始 SQL 插入明文组合
    from django.utils import timezone as dj_tz
    now = dj_tz.now()
    guest_ids = {}
    with connection.cursor() as cur:
        for full_name, phone, normalized in (
                ("演练-仅原始号", "138-0000-2001", None),
                ("演练-仅标准化号", "", "13800002002"),
                ("演练-双空串", "", ""),
                ("演练-空串加NULL", "", None)):
            cur.execute(
                "INSERT INTO guests_guest (full_name, phone, phone_normalized, "
                "email, notes, consent_version, created_at, updated_at, "
                "property_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (full_name, phone, normalized, "", "", "WB124-DRILL-M",
                 now, now, prop.pk))
            guest_ids[full_name] = cur.lastrowid
        g1 = guest_ids["演练-仅原始号"]
        cur.execute(
            "INSERT INTO guests_guestphone (guest_id, phone, phone_normalized, "
            "phone_type, is_primary, shared_review_required, source, "
            "created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (g1, "138-0000-2003", "", "MOBILE", 0, 0, "manual", now, now))
        gp_real = cur.lastrowid
        cur.execute(
            "INSERT INTO guests_guestphone (guest_id, phone, phone_normalized, "
            "phone_type, is_primary, shared_review_required, source, "
            "created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (g1, "", "", "MOBILE", 0, 0, "manual", now, now))
        gp_empty = cur.lastrowid
    expected = {
        guest_ids["演练-仅原始号"]: "13800002001",   # phone-only：原始值标准化
        guest_ids["演练-仅标准化号"]: "13800002002",  # normalized-only
        guest_ids["演练-双空串"]: None,               # ("","")：无号码
        guest_ids["演练-空串加NULL"]: None,           # ("",NULL)：无号码
    }
    gp_expected = {gp_real: "13800002003", gp_empty: None}
    with connection.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM guests_guest WHERE property_id=%s",
                    [prop.pk])
        n_guest = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM guests_guestphone gp "
            "JOIN guests_guest g ON g.id=gp.guest_id WHERE g.property_id=%s",
            [prop.pk])
        n_phone = cur.fetchone()[0]
    check("迁移前明文数据就位（演练物业）", n_guest == 4 and n_phone == 2,
          f"Guest={n_guest} GuestPhone={n_phone}")

    # ---------- Phase 2: 迁移前备份（mysqldump） ----------
    step("迁移前备份：mysqldump --single-transaction 全库备份")
    connection.close()
    dump_args = [MYSQLDUMP, *COMMON_ARGS, "--single-transaction",
                 "--triggers", "--routines", "--set-gtid-purged=OFF",
                 DB_NAME]
    with open(BACKUP_FILE, "wb") as fh:
        r = subprocess.run(dump_args, stdout=fh, stderr=subprocess.PIPE,
                           text=True)
    dump_size = os.path.getsize(BACKUP_FILE) if os.path.exists(BACKUP_FILE) else 0
    check("mysqldump 备份完成", r.returncode == 0 and dump_size > 1000,
          f"rc={r.returncode} size={dump_size} "
          f"{(r.stderr or '')[-200:]}" if r.returncode else f"size={dump_size}")
    with open(BACKUP_FILE, "r", encoding="utf-8", errors="ignore") as fh:
        dump_head = fh.read(200000)
    check("备份含 guests_guest 表定义与数据",
          "`guests_guest`" in dump_head and "138-0000-2001" in dump_head)
    r = sql_cli(DB_NAME, sql=f"SELECT COUNT(*) FROM guests_guest "
                             f"WHERE property_id={prop.pk}")
    check("备份后源库行数核对（演练物业）", r.stdout.strip().endswith("4"),
          r.stdout.strip()[-60:])

    # ---------- Phase 3: 前向迁移 + 迁移后核对 ----------
    step("前向迁移 guests 至最新（0007 回填+清明文）并核对")
    call_command("migrate", "guests", verbosity=0, interactive=False)
    verify_backfill(expected, gp_expected, "首次迁移", prop)

    # ---------- Phase 4: 灾难恢复（DROP + 备份恢复） ----------
    step(f"灾难恢复：DROP DATABASE → mysql < {BACKUP_FILE} 恢复")
    connection.close()
    r = sql_cli("", sql=f"DROP DATABASE {DB_NAME}; "
                        f"CREATE DATABASE {DB_NAME} CHARACTER SET utf8mb4 "
                        f"COLLATE utf8mb4_unicode_ci;")
    check("故障模拟：库已删除并重建空库", r.returncode == 0,
          r.stderr.strip()[-200:])
    r = sql_cli(DB_NAME, input_file=BACKUP_FILE)
    check("mysql 客户端恢复备份完成", r.returncode == 0,
          r.stderr.strip()[-200:] if r.returncode else "")
    check("恢复后 guests 迁移状态回到 0006",
          (guests_migration_state() or "").startswith("0006"),
          guests_migration_state())
    with connection.cursor() as cur:
        cur.execute(
            "SELECT g.full_name, g.phone, g.phone_normalized FROM guests_guest g "
            "WHERE g.property_id=%s ORDER BY g.id", [prop.pk])
        restored = cur.fetchall()
    check("恢复后行数与迁移前一致（演练物业）", len(restored) == 4,
          f"rows={len(restored)}")
    restored_plain_ok = bool(restored) and restored[0][1] == "138-0000-2001"
    check("恢复后原始明文数据完整（备份可用）", restored_plain_ok,
          str(restored))

    # ---------- Phase 5: 恢复后重放迁移 + 二次核对 ----------
    step("恢复后重放迁移并二次核对")
    call_command("migrate", "guests", verbosity=0, interactive=False)
    verify_backfill(expected, gp_expected, "恢复重放", prop)

    # ---------- 结论 ----------
    print("--- 演练结论 ---", flush=True)
    if FAILURES:
        print(f"RESULT: FAILED（{len(FAILURES)} 项未通过）")
        for item in FAILURES:
            print(f"  - {item}")
        sys.exit(1)
    print("RESULT: PASS（MySQL 备份→迁移→恢复→重放→核对 全链路通过）")


if __name__ == "__main__":
    main()
