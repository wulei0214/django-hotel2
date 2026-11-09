# -*- coding: utf-8 -*-
from django.db import migrations


def grant_pii_permissions(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    content_type, _ = ContentType.objects.get_or_create(
        app_label="hotelapp", model="client")
    permissions = []
    for codename, name in (
        ("guest_document_reveal", "能揭示客人证件完整值"),
        ("guest_phone_reveal", "能揭示客人电话号码完整值"),
    ):
        permission, _ = Permission.objects.get_or_create(
            content_type=content_type, codename=codename,
            defaults={"name": name})
        permissions.append(permission)
    for role in Group.objects.filter(name__in=("前台", "值班经理", "店长")):
        role.permissions.add(*permissions)


def revoke_pii_permissions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    try:
        ct = ContentType.objects.get(app_label="hotelapp", model="client")
    except ContentType.DoesNotExist:
        return
    Permission.objects.filter(
        content_type=ct,
        codename__in=("guest_document_reveal", "guest_phone_reveal"),
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("hotelapp", "0016_unify_authorization")]
    operations = [migrations.RunPython(grant_pii_permissions, revoke_pii_permissions)]
