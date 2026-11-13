# -*- coding: utf-8 -*-
"""P0-04（闭环补齐 2.0）：审计时间线独立只读权限 audit.view。

audit_timeline 从 identity.staff_manage（员工管理权）纠偏为
core.audit_view（审计查看专项权）——员工管理权与审计查看是不同职责。
授予：店长、值班经理（与既有审计相关授权口径一致）。
回滚：reverse 移除组授权（权限点保留）。
"""
from django.db import migrations


def grant(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    GroupPermission = Group.permissions.through
    ct, _ = ContentType.objects.get_or_create(app_label="core", model="auditevent")
    perm, _ = Permission.objects.get_or_create(
        content_type=ct, codename="audit_view", defaults={"name": "能查看审计时间线与合规记录"}
    )
    for name in ("店长", "值班经理"):
        group = Group.objects.filter(name=name).first()
        if group:
            GroupPermission.objects.get_or_create(group_id=group.pk, permission_id=perm.pk)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    GroupPermission = Group.permissions.through
    try:
        ct = ContentType.objects.get(app_label="core", model="auditevent")
        perm = Permission.objects.get(content_type=ct, codename="audit_view")
    except (ContentType.DoesNotExist, Permission.DoesNotExist):
        return
    GroupPermission.objects.filter(permission_id=perm.pk).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0009_add_rate_limit_counter")]
    operations = [migrations.RunPython(grant, revoke)]
