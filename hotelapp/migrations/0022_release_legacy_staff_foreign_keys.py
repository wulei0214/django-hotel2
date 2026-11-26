# -*- coding: utf-8 -*-
"""ARCH-04 C6：解除 Food/Order/Room 三处对旧 staff 表的反向外键。

方案口径（docs/acceptance/ARCH-04LegacyStaff迁移方案_2026-11-26.md §5.2）：
旧表退役的前置条件是「正向迁移 + 可用反向迁移（不可 noop）」。本迁移把三处
``ForeignKey('Staff', DO_NOTHING)`` 改为可空的普通整数字段：

* 旧行上的工号原值原样保留（同一列、同一值），不删任何历史数据行；
* 数据库不再存在指向 staff 表的外键约束——旧表将来可物理退役而无需先改约束；
* 反向迁移由 Django 自动还原为原 FK 定义（约束可重建，前提是旧表行仍在，
  与「不删旧表、不删历史数据」口径一致）。

迁移不含 RunPython（无数据搬运）：列由 FK 改为整数时原值不变，
逐行数据在 schema 变更中原样保留。
"""
from django.db import migrations, models


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
    ]
