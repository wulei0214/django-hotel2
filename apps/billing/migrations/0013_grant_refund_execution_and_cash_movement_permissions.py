# -*- coding: utf-8 -*-
"""退款执行与班次现金变动权限：申请、审批、执行三方职责分离。"""
from django.db import migrations


def grant(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    refund_ct, _ = ContentType.objects.get_or_create(app_label="billing", model="refund")
    execute, _ = Permission.objects.get_or_create(
        content_type=refund_ct, codename="refund_execute", defaults={"name": "可以执行已批准退款"}
    )
    finance = Group.objects.filter(name="财务").first()
    if finance:
        finance.permissions.add(execute)

    shift_ct, _ = ContentType.objects.get_or_create(app_label="billing", model="cashiershift")
    movement, _ = Permission.objects.get_or_create(
        content_type=shift_ct,
        codename="cash_movement_manage",
        defaults={"name": "可以登记班次现金收入与支出"},
    )
    if finance:
        finance.permissions.add(movement)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    for app_label, model, codename in (
        ("billing", "refund", "refund_execute"),
        ("billing", "cashiershift", "cash_movement_manage"),
    ):
        ct = ContentType.objects.filter(app_label=app_label, model=model).first()
        if ct is None:
            continue
        permission = Permission.objects.filter(content_type=ct, codename=codename).first()
        if permission is not None:
            for group in Group.objects.all():
                group.permissions.remove(permission)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0012_refund_execution_and_shift_cash_movements"),
    ]

    operations = [migrations.RunPython(grant, revoke)]
