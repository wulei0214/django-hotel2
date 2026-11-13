# -*- coding: utf-8 -*-
from django.db import migrations


def grant_rate_management(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    ct, _ = ContentType.objects.get_or_create(app_label="inventory", model="rateplan")
    permission, _ = Permission.objects.get_or_create(
        content_type=ct, codename="manage_rates", defaults={"name": "能管理酒店每日房价"}
    )
    manager = Group.objects.filter(name="店长").first()
    if manager:
        manager.permissions.add(permission)


def revoke_rate_management(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    ct = ContentType.objects.filter(app_label="inventory", model="rateplan").first()
    if ct:
        permission = Permission.objects.filter(content_type=ct, codename="manage_rates").first()
        if permission:
            for group in Group.objects.all():
                group.permissions.remove(permission)
            permission.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0003_housekeepingtask_roominspection_maintenanceticket_and_more"),
        ("hotelapp", "0016_unify_authorization"),
    ]
    operations = [
        migrations.AlterModelOptions(
            name="rateplan",
            options={
                "verbose_name": "房价计划",
                "verbose_name_plural": "房价计划",
                "ordering": ("code",),
                "permissions": [("manage_rates", "能管理酒店每日房价")],
            },
        ),
        migrations.RunPython(grant_rate_management, revoke_rate_management),
    ]
