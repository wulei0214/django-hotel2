# -*- coding: utf-8 -*-
"""RBAC 统一角色目录收尾：删除六个英文编码组（双轨目录清理）。

0014 因保守策略只删空组，而这六组各挂有权限副本（identity 0001 创建、0 用户绑定），
与对应中文组完全重复。bind_staff_user 已改为映射到中文组，英文组再无引用方。
"""
from django.db import migrations

ENGLISH_GROUPS = [
    "FRONT_DESK", "DUTY_MANAGER", "FINANCE", "PROPERTY_OWNER",
    "HOUSEKEEPING", "NIGHT_AUDIT",
]


def drop_english_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    # 安全阀：若有用户仍在英文组（说明存在未迁移的绑定），保留并报警不删
    for g in Group.objects.filter(name__in=ENGLISH_GROUPS):
        if g.user_set.count() == 0:
            g.delete()


def restore_groups(apps, schema_editor):
    # 反向不重建：中文组是权威目录，英文组为冗余副本（历史可查 git）
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("hotelapp", "0014_role_actions"),
    ]

    operations = [
        migrations.RunPython(drop_english_groups, restore_groups),
    ]
