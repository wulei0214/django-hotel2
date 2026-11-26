# -*- coding: utf-8 -*-
"""ARCH-04 C6：解除 Food/Order/Room 三处对旧 staff 表的反向外键。

方案口径（docs/acceptance/ARCH-04LegacyStaff迁移方案_2026-11-26.md §5.2）：
旧表退役的前置条件是「正向迁移 + 可用反向迁移（不可 noop）」。本迁移把三处
``ForeignKey('Staff', DO_NOTHING)`` 改为可空的普通整数字段：

* 旧行上的工号原值原样保留（同一列、同一值），不删任何历史数据行；
* 数据库不再存在指向 staff 表的外键约束——旧表将来可物理退役而无需先改约束；
* 反向迁移把三列还原为原 FK 定义（约束可重建），且**在真实数据上可用**。

反向可用性（2026-11-26 复核修正）
--------------------------------
前一版把反向能力写成「由 Django 自动还原，前提是旧表行仍在」，并在同批用例里
把「悬空工号（指向不存在的 staff 行）」与「NULL 工号」断言为合法数据——这两件事
互相矛盾：反向迁移在 MySQL 上先 ``MODIFY ... NOT NULL``（遇 NULL 报 1138
Invalid use of NULL value），再 ``ADD CONSTRAINT ... FOREIGN KEY``（遇悬空工号
报 1452）。实证（隔离 MySQL 库上真实跑反向）确认：在含 NULL/悬空工号的数据上
反向迁移**必失败**，即原 docstring 的「可用反向迁移」承诺不成立。

本版把数据修复放进迁移的反向路径（RunPython 置于操作列表末尾——Django 反向时
按操作倒序撤销，末尾的 RunPython 会**先于** schema 变更执行）：

1. 为每列中指向不存在 staff 行的工号，补建同 id 的占位员工行（保留原工号值，
   使历史快照里的工号仍可在旧表查到对应行）；
2. 为每列中的 NULL 工号，补建一个固定 id 的占位员工行并回填。

占位员工行只在回滚时产生，姓名以「占位员工」开头、启用=False，便于识别与清理；
正向迁移不做任何数据搬运（列由 FK 改为整数时原值不变，逐行数据原样保留）。
"""
from django.db import migrations, models

# 回滚时用于承载 NULL 工号的占位员工 id（staff.员工id 为自增主键，不与业务工号冲突）。
# 取 -1：Django 拒绝 AutoField 显式取 0（"does not accept 0 as a value for AutoField"），
# 负值不占用自增序列，也不会与真实工号相撞。
_SENTINEL_STAFF_ID = -1

# 三处被解除的反向外键：(表名, 列名)
_C6_COLUMNS = (
    ("food", "厨师id"),
    ("order", "服务员编号"),
    ("room", "负责人编号"),
)


def _noop_forward(apps, schema_editor):
    """正向不做数据搬运（列定义变更本身不改行值）。"""
    return None


def _repair_before_reverse(apps, schema_editor):
    """反向 schema 变更之前的数据修复：让 FK 约束可被真实重建。

    见模块 docstring。修复只补建占位员工行并回填 NULL，不删除、不改写任何
    既有业务行的真实工号值。
    """
    Staff = apps.get_model("hotelapp", "Staff")
    connection = schema_editor.connection

    def _staff_exists(cursor, staff_id):
        cursor.execute("SELECT 1 FROM `staff` WHERE `员工id` = %s", [staff_id])
        return cursor.fetchone() is not None

    def _create_placeholder(staff_id):
        # 历史模型的主键字段名就是「员工id」（AutoField），没有独立的 id 字段
        Staff.objects.create(
            **{
                "员工id": staff_id,
                "姓名": "占位员工%s" % staff_id,
                "启用": False,
                "职务": "占位",
                "月薪": "0",
                "工龄": "0",
            }
        )

    with connection.cursor() as cursor:
        # ① 悬空工号：补建同 id 的占位员工行（保留工号原值）
        for table, column in _C6_COLUMNS:
            cursor.execute(
                "SELECT DISTINCT `{column}` FROM `{table}` WHERE `{column}` IS NOT NULL".format(
                    table=table, column=column
                )
            )
            for (value,) in cursor.fetchall():
                if value is not None and not _staff_exists(cursor, value):
                    _create_placeholder(value)

        # ② NULL 工号：补建固定 id 的占位员工行并回填
        if not _staff_exists(cursor, _SENTINEL_STAFF_ID):
            _create_placeholder(_SENTINEL_STAFF_ID)
        for table, column in _C6_COLUMNS:
            cursor.execute(
                "UPDATE `{table}` SET `{column}` = %s WHERE `{column}` IS NULL".format(
                    table=table, column=column
                ),
                [_SENTINEL_STAFF_ID],
            )


class Migration(migrations.Migration):

    dependencies = [
        ("hotelapp", "0021_night_auditor_activation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="food",
            name="厨师id",
            field=models.IntegerField(blank=True, db_column="厨师id", help_text="旧 staff 表员工工号（历史快照，已非外键）", null=True, verbose_name="负责厨师"),
        ),
        migrations.AlterField(
            model_name="order",
            name="服务员编号",
            field=models.IntegerField(blank=True, db_column="服务员编号", help_text="旧 staff 表员工工号（历史快照，已非外键）", null=True, verbose_name="服务员工"),
        ),
        migrations.AlterField(
            model_name="room",
            name="负责人编号",
            field=models.IntegerField(blank=True, db_column="负责人编号", help_text="旧 staff 表员工工号（历史快照，已非外键）", null=True, verbose_name="保洁负责人"),
        ),
        # 置于操作列表末尾：反向时先于上面的 schema 变更执行数据修复，
        # 使「可用反向迁移」在含 NULL/悬空工号的真实数据上成立。
        migrations.RunPython(_noop_forward, _repair_before_reverse),
    ]
