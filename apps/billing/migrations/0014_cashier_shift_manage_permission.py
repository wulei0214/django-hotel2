# -*- coding: utf-8 -*-
"""收银班次管理权限：前台必须能开闭本人班次，财务/管理者可监督。"""
from django.db import migrations, models


def grant(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct, _ = ContentType.objects.get_or_create(
        app_label="billing", model="cashiershift")
    permission, _ = Permission.objects.get_or_create(
        content_type=ct, codename="cashier_shift_manage",
        defaults={"name": "可以开立和关闭收银班次"})
    for role_name in ("前台", "财务", "值班经理", "店长"):
        group = Group.objects.filter(name=role_name).first()
        if group:
            group.permissions.add(permission)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct = ContentType.objects.filter(
        app_label="billing", model="cashiershift").first()
    if ct is None:
        return
    permission = Permission.objects.filter(
        content_type=ct, codename="cashier_shift_manage").first()
    if permission is not None:
        for group in Group.objects.all():
            group.permissions.remove(permission)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0013_grant_refund_execution_and_cash_movement_permissions"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="cashiershift",
            options={
                "verbose_name": "现金班次", "verbose_name_plural": "现金班次",
                "ordering": ("-opened_at",),
                "permissions": (
                    ("cashier_shift_manage", "可以开立和关闭收银班次"),
                    ("cash_movement_manage", "可以登记班次现金收入与支出"),
                ),
            },
        ),
        migrations.RunPython(grant, revoke),
    ]
