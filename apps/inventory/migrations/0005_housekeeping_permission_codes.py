# -*- coding: utf-8 -*-
"""清洁/维修工作台专项权限拆分，保持既有角色授权集合不扩大。

housekeeping_view    <- 既有 hotelapp.view_room 组授权
housekeeping_update  <- 既有 hotelapp.change_room 组授权
maintenance_manage   <- 既有 hotelapp.no_show_handle 组授权

迁移只复制原授权组，不授予新角色；维修权限从未到处理权限中独立出来。
"""
from django.db import migrations


PERMISSIONS = (
    ("housekeeping_view", "可以查看清洁与维修工作台", "view_room"),
    ("housekeeping_update", "可以流转客房清洁状态", "change_room"),
    ("maintenance_manage", "可以登记和恢复客房维修状态", "no_show_handle"),
)


def grant_housekeeping_permissions(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    GroupPermission = Group.permissions.through

    room_ct, _ = ContentType.objects.get_or_create(
        app_label="inventory", model="room")

    for new_codename, label, legacy_codename in PERMISSIONS:
        new_permission, _ = Permission.objects.get_or_create(
            content_type=room_ct,
            codename=new_codename,
            defaults={"name": label},
        )
        old_permission = Permission.objects.filter(
            content_type__app_label="hotelapp",
            codename=legacy_codename,
        ).first()
        if old_permission is None:
            continue
        group_ids = GroupPermission.objects.filter(
            permission_id=old_permission.pk
        ).values_list("group_id", flat=True)
        for group_id in group_ids.iterator():
            GroupPermission.objects.get_or_create(
                group_id=group_id, permission_id=new_permission.pk)


def keep_grants_on_reverse(apps, schema_editor):
    # Add-only 权限映射：回滚不撤销后续经权限中心审核过的授权。
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0004_rateplan_manage_rates"),
        ("hotelapp", "0020_restore_room_release_ready_execution"),
        ("identity", "0006_invitation_invitation_idx_invitation_profile_status_and_more"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="room",
            options={
                "verbose_name": "物理客房",
                "verbose_name_plural": "物理客房",
                "ordering": ("number",),
                "permissions": (
                    ("housekeeping_view", "可以查看清洁与维修工作台"),
                    ("housekeeping_update", "可以流转客房清洁状态"),
                    ("maintenance_manage", "可以登记和恢复客房维修状态"),
                ),
            },
        ),
        migrations.RunPython(
            grant_housekeeping_permissions, keep_grants_on_reverse),
    ]
