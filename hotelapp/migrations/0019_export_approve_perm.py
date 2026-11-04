# -*- coding: utf-8 -*-
"""F-06（治理方案 Phase 5 §10.2）：敏感导出审批权限码化。

此前 approve_staff_export 用 _has_role("值班经理","店长") 硬编码判定，
权限矩阵无法管理该动作。本迁移新增 hotelapp.export_approve 权限码并按
**现有语义原样**授予值班经理/店长（财务不含——审批语义不变，无放宽），
代码判定改走 AuthorizationService，矩阵即可管理。

等价性说明：授予集 = 原硬编码集合，行为等价；回滚 = reverse 移除授权。
"""
from django.db import migrations


def grant(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct, _ = ContentType.objects.get_or_create(
        app_label="hotelapp", model="staff")
    perm, _ = Permission.objects.get_or_create(
        content_type=ct, codename="export_approve",
        defaults={"name": "能审批敏感导出申请"})
    for name in ("值班经理", "店长"):
        group = Group.objects.filter(name=name).first()
        if group:
            group.permissions.add(perm)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct, _ = ContentType.objects.get_or_create(
        app_label="hotelapp", model="staff")
    try:
        perm = Permission.objects.get(
            content_type=ct, codename="export_approve")
    except Permission.DoesNotExist:
        return
    for group in Group.objects.all():
        group.permissions.remove(perm)


class Migration(migrations.Migration):
    dependencies = [
        ("hotelapp", "0018_finance_view_staff_readonly"),
    ]
    operations = [migrations.RunPython(grant, revoke)]
