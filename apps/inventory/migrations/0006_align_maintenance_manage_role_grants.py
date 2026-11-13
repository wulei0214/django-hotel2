# -*- coding: utf-8 -*-
"""纠正维修权限的历史授权污染。

0005 曾把 hotelapp.no_show_handle 的组授权复制到 maintenance_manage。
未到处理与维修管理是不同业务动作；本迁移撤销错误继承，并按已批准的
PMS RBAC 决策只授予值班经理、店长。客房服务员仅保留清洁流转权限。
"""
from django.db import migrations


def align_maintenance_grants(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    content_type, _ = ContentType.objects.get_or_create(
        app_label="inventory", model="room")
    permission, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename="maintenance_manage",
        defaults={"name": "可以登记和恢复客房维修状态"},
    )

    # 0005 的来源映射不可信：清除所有组上的错误复制结果，再按正式角色矩阵重授。
    Group.permissions.through.objects.filter(permission_id=permission.pk).delete()

    for role_name in ("值班经理", "店长"):
        group = Group.objects.filter(name=role_name).first()
        if group is not None:
            Group.permissions.through.objects.get_or_create(
                group_id=group.pk, permission_id=permission.pk)


def keep_approved_grants_on_reverse(apps, schema_editor):
    # 保留已核准的最小授权，不恢复历史错误的 no_show_handle 映射。
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0005_housekeeping_permission_codes"),
        ("hotelapp", "0014_role_actions"),
    ]

    operations = [
        migrations.RunPython(
            align_maintenance_grants, keep_approved_grants_on_reverse),
    ]
