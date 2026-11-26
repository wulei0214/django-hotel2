# -*- coding: utf-8 -*-
"""ARCH-04 C7：StaffProfile.legacy_staff 由 PROTECT 改为 SET_NULL（可空关联）。

旧员工表渐进退役：历史映射行保留（关联可追溯），但不再阻断旧表处置——
旧行被清理时关联置空，而不是抛 ProtectedError。

反向迁移由 Django 自动还原为 PROTECT 定义（不是 noop）：回滚能力真实可用，
旧表行仍在时约束可原样重建。
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("hotelapp", "0022_release_legacy_staff_foreign_keys"),
        ("identity", "0013_ns_e05_permission_grants"),
    ]

    operations = [
        migrations.AlterField(
            model_name="staffprofile",
            name="legacy_staff",
            field=models.OneToOneField(
                blank=True,
                help_text="与 hotelapp 员工表的历史映射；未迁移的员工为空；旧表退役后置空不阻断处置",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="pms_staff_profile",
                to="hotelapp.staff",
                verbose_name="旧版员工记录",
            ),
        ),
    ]
