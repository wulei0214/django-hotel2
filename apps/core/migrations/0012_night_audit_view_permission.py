# -*- coding: utf-8 -*-
"""夜审只读权限与执行权限分离。

core.night_audit_view 仅控制夜审中心页面、预览、营业日/运行历史/break-glass
只读查询。既有可查看角色由原 hotelapp.night_audit_execute 授权组复制，
不扩大可见范围；夜审执行仍由 hotelapp.night_audit_execute 控制。
"""
from django.db import migrations


def grant_night_audit_view(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    GroupPermission = Group.permissions.through

    content_type, _ = ContentType.objects.get_or_create(
        app_label="core", model="businessdate")
    view_permission, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename="night_audit_view",
        defaults={"name": "可以查看营业日与夜审记录"},
    )

    execute_permission = Permission.objects.filter(
        content_type__app_label="hotelapp",
        codename="night_audit_execute",
    ).first()
    if execute_permission is None:
        return

    # 保持原授权人群不变：只读权限复制旧执行权限的既有组，不给新角色扩权。
    source_group_ids = GroupPermission.objects.filter(
        permission_id=execute_permission.pk
    ).values_list("group_id", flat=True)
    for group_id in source_group_ids.iterator():
        GroupPermission.objects.get_or_create(
            group_id=group_id, permission_id=view_permission.pk)


def keep_grants_on_reverse(apps, schema_editor):
    # Add-only 授权迁移：回滚不撤销可能已由平台权限中心重新审核的组授权。
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0011_notification_notification_idx_notif_rcpt_read_time_and_more"),
        ("hotelapp", "0020_restore_room_release_ready_execution"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="businessdate",
            options={
                "verbose_name": "营业日",
                "verbose_name_plural": "营业日",
                "permissions": (
                    ("breakglass_review", "可以复核紧急营业日重开"),
                    ("night_audit_view", "可以查看营业日与夜审记录"),
                ),
            },
        ),
        migrations.RunPython(grant_night_audit_view, keep_grants_on_reverse),
    ]
