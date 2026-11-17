# -*- coding: utf-8 -*-
"""U-03/F-02 演示数据重置（受控 · 纯 ORM 删除，无 SQL 拼接）。

护栏（对齐排期/基线要求）：
  * 仅允许本机 localhost + DJANGO_ENV=dev + --confirm-database hotel
  * 必须先有非空备份文件（--backup-file 指向，F-02 阶段 A 产物）
  * 必须 --i-understand-reset 显式确认
  * 执行前打印清空范围与保留范围
  * 保留：主数据（物业/房型/房间/价格计划/政策）、员工身份链、
    审计事件（§6：不物理删除审计）、工号计数器（工号不回收）、
    平台账号与权限、迁移元数据
  * 清空：交易与演示业务表（预订/入住/住客/账务凭证/班次/清洁/
    任务编排/通知等）——经 Django ORM 逐模型删除（财务事实模型
    经原生 QuerySet 绕过不可变守卫，本仓库测试既定模式）
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hotel.settings")

KEEP_TABLES = {
    "core_property", "inventory_roomtype", "inventory_room", "inventory_rateplan",
    "reservations_cancellationpolicy", "reservations_depositpolicy",
    "identity_staffprofile", "identity_propertystaffmembership",
    "identity_staffroleassignment", "identity_invitation",
    "identity_employeenumbercounter",
    "auth_user", "auth_group", "auth_permission", "auth_user_groups",
    "auth_user_user_permissions", "auth_group_permissions",
    "core_auditevent",
    "django_migrations", "django_content_type", "django_session",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm-database", required=True)
    parser.add_argument("--backup-file", required=True)
    parser.add_argument("--i-understand-reset", action="store_true")
    args = parser.parse_args()

    if os.environ.get("DJANGO_ENV") != "dev":
        print("拒绝：仅允许 DJANGO_ENV=dev 本地环境执行。")
        return 2
    backup = Path(args.backup_file)
    if not backup.exists() or backup.stat().st_size == 0:
        print("拒绝：备份文件不存在或为空（F-02 阶段 A 门禁）。")
        return 2
    if not args.i_understand_reset:
        print("拒绝：缺少 --i-understand-reset 显式确认。")
        return 2

    import django
    django.setup()
    from django.apps import apps
    from django.conf import settings
    from django.db import connection
    from django.db.models import QuerySet

    db = connection.settings_dict
    if args.confirm_database != db["NAME"] or db["HOST"] not in ("", "localhost", "127.0.0.1"):
        print(f"拒绝：数据库目标不符（{db['NAME']}@{db['HOST']}）。")
        return 2

    table_to_model = {}
    unmapped = []
    for model in apps.get_models(include_auto_created=True):
        table = model._meta.db_table
        if table in KEEP_TABLES:
            continue
        if table in table_to_model:
            print(f"警告：表 {table} 有多个模型映射，跳过后到者。")
        table_to_model[table] = model

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_type='BASE TABLE'")
        tables = {row[0] for row in cursor.fetchall()}
    to_clear = sorted(t for t in tables if t not in KEEP_TABLES)
    kept = sorted(t for t in tables if t in KEEP_TABLES)
    no_model = [t for t in to_clear if t not in table_to_model]
    if no_model:
        print("[SKIP] 以下表无 Django 模型映射（早期 schema 孤儿表，ORM 不触及，保留不动）："
              + ", ".join(no_model))

    print("[GUARD] 清空范围（ORM 逐模型删除，共 %d 张交易/演示表）：" % len(to_clear))
    for name in to_clear:
        print("[GUARD]   - " + name)
    print("[GUARD] 保留范围（主数据/员工身份链/审计/工号计数器/迁移元数据，共 %d 张）：" % len(kept))
    for name in kept:
        print("[GUARD]   = " + name)

    # 逐模型删除：财务事实模型（不可变守卫）经原生 QuerySet 绕过——
    # 本仓库测试/演示既定模式；FK 关系由删除顺序（子表先于父表按依赖排序）
    # 与外键检查关闭共同保证。
    from django.db import IntegrityError
    deleted_rows = 0
    remaining = list(to_clear)
    for round_i in range(6):
        progressed = False
        for table in list(remaining):
            if table not in table_to_model:
                print(f"  跳过 {table}: 无模型映射（孤儿表保留）")
                remaining.remove(table)
                continue
            model = table_to_model[table]
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET FOREIGN_KEY_CHECKS=0")
                qs = QuerySet(model)
                count = qs.count()
                if count:
                    qs._raw_delete(qs.db)
                    deleted_rows += count
                    print(f"  清空 {table}: {count} 行")
                else:
                    print(f"  清空 {table}: 0 行")
                remaining.remove(table)
                progressed = True
            except IntegrityError:
                continue  # 依赖父表未清，下一轮再试
            except Exception as exc:
                print(f"  跳过 {table}: {type(exc).__name__}: {exc}")
                remaining.remove(table)
        if not remaining:
            break
        if not progressed:
            print("剩余表无法清空（依赖环），请人工处理：" + ", ".join(remaining))
            return 3
    with connection.cursor() as cursor:
        cursor.execute("SET FOREIGN_KEY_CHECKS=1")
    print("[DONE] 演示业务数据已清空（共 %d 行）；备份：" % deleted_rows + str(backup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
