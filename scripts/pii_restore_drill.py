# -*- coding: utf-8 -*-
"""WB-124：手机号 PII 迁移——隔离环境备份恢复演练（第 31 轮）。

在一次性 SQLite 隔离库中完整演练「迁移前备份 → 前向迁移 → 灾难恢复 →
恢复后重放迁移 → 二次核对」，不触碰 dev/prod 数据库：

  Phase 1  初始化：migrate guests 0006（依赖自动应用），写入迁移前明文数据
           （覆盖 phone-only / normalized-only / 三种空字符串组合）
  Phase 2  迁移前备份：文件级备份 + SQLite integrity_check + 行数核对
  Phase 3  前向迁移：应用 guests 0007（回填+清明文），核对数量、明文清除、
           解密、HMAC，并经业务读取入口 guest_phone_plain 验证
  Phase 4  灾难恢复：删除工作库，用迁移前备份恢复，核对恢复后行数与
           原始明文数据完整（备份可用性验证）
  Phase 5  恢复后重放迁移：再次前向迁移，二次核对解密 / HMAC / 数量

每步输出 [PASS]/[FAIL]；任何失败以非零退出码结束（演练不通过）。
运行方式（PowerShell，env 注入）：
  $env:DJANGO_ENV='dev'; $env:DJANGO_DB_ENGINE='sqlite';
  $env:DJANGO_DB_NAME='<scratch 文件>'; python scripts/pii_restore_drill.py
"""
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hotel.settings")

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402

from apps.core.models import Property  # noqa: E402
from apps.guests import services as guest_services  # noqa: E402
from apps.guests.models import Guest, GuestPhone  # noqa: E402

FAILURES = []
STEP = [0]


def step(title):
    STEP[0] += 1
    print(f"--- Phase {STEP[0]}: {title}")


def check(label, ok, detail=""):
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}" + (f" | {detail}" if detail else ""))
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def verify_backfill(expected, gp_expected, tag):
    """核对回填/清明文结果：数量、明文清除、解密、HMAC、业务读取。"""
    rows_ok = (Guest.objects.count() == len(expected)
               and GuestPhone.objects.count() == len(gp_expected))
    check(f"{tag}：迁移后行数一致", rows_ok,
          f"Guest={Guest.objects.count()} GuestPhone={GuestPhone.objects.count()}")
    for model, mapping in ((Guest, expected), (GuestPhone, gp_expected)):
        for pk, plain in mapping.items():
            row = model.objects.get(pk=pk)
            if plain is None:
                ok = (not (row.phone_ciphertext or "")
                      and not (row.phone_hash or ""))
                check(f"{tag}：无号码记录不伪造密文 "
                      f"model={model.__name__}#{pk}", ok)
            else:
                ok = (row.phone_ciphertext and row.phone_hash
                      and guest_services.decrypt_phone(
                          row.phone_ciphertext) == plain
                      and row.phone_hash == guest_services.phone_hash(plain))
                check(f"{tag}：密文可解密且 HMAC 一致 "
                      f"model={model.__name__}#{pk}", ok, plain)
            if row.phone is not None or row.phone_normalized is not None:
                check(f"{tag}：明文已清除 model={model.__name__}#{pk}",
                      False,
                      f"phone={row.phone!r} "
                      f"normalized={row.phone_normalized!r}")
    # 业务读取入口：有号码的解密回明文，无号码的返回空串（口径同服务层）
    biz_ok = all(
        guest_services.guest_phone_plain(Guest.objects.get(pk=pk))
        == (plain if plain is not None else "")
        for pk, plain in expected.items())
    check(f"{tag}：业务读取 guest_phone_plain 正常", biz_ok)


def main():
    work_db = connection.settings_dict["NAME"]
    backup_db = work_db + ".bak"
    for path in (work_db, backup_db):
        if os.path.exists(path):
            os.remove(path)

    # ---------- Phase 1: 初始化迁移前状态（0006） ----------
    step("初始化隔离库至 guests 0006（迁移前状态）")
    call_command("migrate", "guests", "0006", verbosity=0, interactive=False)
    prop, _ = Property.objects.get_or_create(
        code="DRILL", defaults={"name": "WB-124 恢复演练物业"})
    # 0006 时刻 guests_guest.phone 为 NOT NULL（列放宽发生在 0007 内），
    # 迁移前数据用原始 SQL 按 0006 时刻 DDL 插入，覆盖全部明文组合：
    # phone-only / normalized-only / ("","") / ("",NULL) / GuestPhone 空串。
    from django.utils import timezone as dj_tz
    now = dj_tz.now().isoformat()
    con = sqlite3.connect(work_db)

    def insert_guest(full_name, phone, normalized):
        cur = con.execute(
            "INSERT INTO guests_guest (full_name, phone, phone_normalized,"
            " email, notes, consent_version, created_at, updated_at,"
            " property_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (full_name, phone, normalized, "", "", "WB124-DRILL",
             now, now, prop.pk))
        return cur.lastrowid

    g1 = insert_guest("演练-仅原始号", "138-0000-2001", None)
    g2 = insert_guest("演练-仅标准化号", "", "13800002002")
    g3 = insert_guest("演练-双空串", "", "")
    g4 = insert_guest("演练-空串加NULL", "", None)

    def insert_phone(guest_id, phone, normalized):
        cur = con.execute(
            "INSERT INTO guests_guestphone (guest_id, phone,"
            " phone_normalized, phone_type, is_primary,"
            " shared_review_required, source, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (guest_id, phone, normalized, "MOBILE", 0, 0, "manual",
             now, now))
        return cur.lastrowid

    gp_empty = insert_phone(g1, "", "")
    gp_real = insert_phone(g1, "138-0000-2003", "")
    con.commit()
    con.close()
    expected = {
        g1: "13800002001",   # phone-only：从原始值标准化
        g2: "13800002002",   # normalized-only
        g3: None,            # ("","")：无号码
        g4: None,            # ("",NULL)：无号码
    }
    gp_expected = {gp_real: "13800002003", gp_empty: None}
    con = sqlite3.connect(work_db)
    n_guest = con.execute("SELECT COUNT(*) FROM guests_guest").fetchone()[0]
    n_phone = con.execute(
        "SELECT COUNT(*) FROM guests_guestphone").fetchone()[0]
    con.close()
    check("迁移前明文数据就位", n_guest == 4 and n_phone == 2,
          f"Guest={n_guest} GuestPhone={n_phone}")

    # ---------- Phase 2: 迁移前备份 ----------
    step("迁移前备份（文件级备份 + 完整性校验）")
    connection.close()
    shutil.copy2(work_db, backup_db)
    con = sqlite3.connect(backup_db)
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    backup_guests = con.execute("SELECT COUNT(*) FROM guests_guest").fetchone()[0]
    backup_phones = con.execute(
        "SELECT COUNT(*) FROM guests_guestphone").fetchone()[0]
    con.close()
    check("备份文件完整性 integrity_check", integrity == "ok", integrity)
    check("备份包含全部迁移前数据", backup_guests == 4 and backup_phones == 2,
          f"guest={backup_guests} guestphone={backup_phones}")

    # ---------- Phase 3: 前向迁移 + 迁移后核对 ----------
    step("前向迁移至最新（0007 回填+清明文）并核对")
    call_command("migrate", "guests", verbosity=0, interactive=False)
    verify_backfill(expected, gp_expected, "首次迁移")

    # ---------- Phase 4: 灾难恢复（迁移前备份覆盖） ----------
    step("灾难恢复：删除工作库并用迁移前备份恢复")
    connection.close()
    os.remove(work_db)
    shutil.copy2(backup_db, work_db)
    con = sqlite3.connect(work_db)
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    restored = con.execute(
        "SELECT full_name, phone, phone_normalized FROM guests_guest "
        "ORDER BY id").fetchall()
    con.close()
    check("恢复库完整性 integrity_check", integrity == "ok", integrity)
    check("恢复后行数与迁移前一致", len(restored) == 4, f"rows={len(restored)}")
    restored_plain_ok = (
        restored[0][1] == "138-0000-2001" and restored[0][2] is None
        and restored[1][1] == "" and restored[1][2] == "13800002002"
        and restored[2][1] == "" and restored[2][2] == ""
        and restored[3][1] == "" and restored[3][2] is None)
    check("恢复后原始明文数据完整（备份可用）", restored_plain_ok,
          str(restored))

    # ---------- Phase 5: 恢复后重放迁移 + 二次核对 ----------
    step("恢复后重放迁移并二次核对")
    call_command("migrate", "guests", verbosity=0, interactive=False)
    verify_backfill(expected, gp_expected, "恢复重放")

    # ---------- 结论 ----------
    print("--- 演练结论 ---")
    if FAILURES:
        print(f"RESULT: FAILED（{len(FAILURES)} 项未通过）")
        for item in FAILURES:
            print(f"  - {item}")
        sys.exit(1)
    print("RESULT: PASS（备份→迁移→恢复→重放→核对 全链路通过）")


if __name__ == "__main__":
    main()
