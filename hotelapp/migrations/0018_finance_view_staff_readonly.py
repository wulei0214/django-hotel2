# -*- coding: utf-8 -*-
from django.db import migrations


def grant_finance_staff_read(apps, schema_editor):
    """Q-03：财务可只读查看员工薪酬；不授予 add/change 权限。"""
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    content_type = ContentType.objects.get(app_label="hotelapp", model="staff")
    permission, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename="view_staff",
        defaults={"name": "Can view 员工信息管理"},
    )
    finance = Group.objects.filter(name="财务").first()
    if finance is not None:
        finance.permissions.add(permission)


def revoke_finance_staff_read(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    try:
        content_type = ContentType.objects.get(
            app_label="hotelapp", model="staff")
    except ContentType.DoesNotExist:
        return
    permission = Permission.objects.filter(
        content_type=content_type, codename="view_staff").first()
    finance = Group.objects.filter(name="财务").first()
    if finance is not None and permission is not None:
        finance.permissions.remove(permission)


class Migration(migrations.Migration):
    dependencies = [
        ("hotelapp", "0017_guest_pii_permissions"),
    ]
    operations = [
        migrations.RunPython(
            grant_finance_staff_read, revoke_finance_staff_read),
    ]
