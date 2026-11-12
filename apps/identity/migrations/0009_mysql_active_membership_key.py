# -*- coding: utf-8 -*-
"""WB-EMP-01 / DEC-EMP-01：用可空唯一键替代 MySQL 8 不支持的条件唯一约束。

identity/0007 建立的 uq_membership_property_staff_active 是
condition=Q(status="ACTIVE") 的部分唯一索引；MySQL 8 会忽略它
（Django models.W036），因此「同一物业同一员工同时点最多一条 ACTIVE 任职」
在 MySQL 上仅有服务层检查，没有数据库级保障。

本迁移沿用项目既有治理方案（core/0006、billing/0006、guests/0006）：
  1. 新增可空唯一列 active_membership_key；
  2. 回填存量 ACTIVE 行（存在重复则显式终止，要求人工先数据修复）；
  3. 仅在 MySQL 上 DROP 从未真正生效的部分索引，并在迁移 state 中
     同步移除条件约束；SQLite/PostgreSQL 上 DROP 已存在的索引。

新数据由 PropertyStaffMembership.save() 统一维护该键：ACTIVE 写入
"{property_id}:{staff_profile_id}"，非 ACTIVE 写 NULL。
"""
from django.db import migrations, models
from django.db.models import Count


def _assert_unique_active(apps, schema_editor):
    Membership = apps.get_model("identity", "PropertyStaffMembership")
    duplicates = list(
        Membership.objects.filter(status="ACTIVE")
        .values("property_id", "staff_profile_id")
        .annotate(total=Count("id"))
        .filter(total__gt=1)[:5]
    )
    if duplicates:
        raise RuntimeError(
            "存在同一物业同一员工的多条 ACTIVE 任职，启用数据库唯一键前"
            f"必须先人工核对并保留唯一有效任职：{duplicates}")


def backfill_active_key(apps, schema_editor):
    _assert_unique_active(apps, schema_editor)
    Membership = apps.get_model("identity", "PropertyStaffMembership")
    rows = list(Membership.objects.filter(status="ACTIVE").values(
        "id", "property_id", "staff_profile_id"))
    for row in rows:
        Membership.objects.filter(pk=row["id"]).update(
            active_membership_key=(
                f"{row['property_id']}:{row['staff_profile_id']}"))


def drop_partial_index(apps, schema_editor):
    model = apps.get_model("identity", "PropertyStaffMembership")
    table = model._meta.db_table
    name = "uq_membership_property_staff_active"
    with schema_editor.connection.cursor() as cursor:
        constraints = schema_editor.connection.introspection.get_constraints(
            cursor, table)
    if name not in constraints:
        # MySQL 上该部分索引从未创建（W036），无需处理。
        return
    q = schema_editor.quote_name
    if schema_editor.connection.vendor == "mysql":
        schema_editor.execute(
            f"ALTER TABLE {q(table)} DROP INDEX {q(name)}")
    else:
        schema_editor.execute(f"DROP INDEX IF EXISTS {q(name)}")


class Migration(migrations.Migration):

    dependencies = [
        ("identity", "0008_employeenumbercounter_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="propertystaffmembership",
            name="active_membership_key",
            field=models.CharField(
                blank=True, editable=False, max_length=64, null=True,
                unique=True, verbose_name="有效任职唯一键"),
        ),
        migrations.RunPython(backfill_active_key, migrations.RunPython.noop),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(drop_partial_index,
                                     migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.RemoveConstraint(
                    model_name="propertystaffmembership",
                    name="uq_membership_property_staff_active",
                ),
            ],
        ),
    ]
