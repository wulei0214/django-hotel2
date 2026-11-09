# -*- coding: utf-8 -*-
"""V1.1 M-02：证件揭示独立权限（guests.document.reveal）。

每次揭示写 AuditEvent；默认不授予任何业务角色（按 D-09 待评审后分配）。
"""
from django.db import migrations


def create_reveal_permission(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct, _ = ContentType.objects.get_or_create(
        app_label="guests", model="guestdocument")
    Permission.objects.get_or_create(
        content_type=ct, codename="document_reveal",
        defaults={"name": "能揭示 客人证件完整值"})


def remove_reveal_permission(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission.objects.filter(
        content_type=ContentType.objects.get(app_label="guests", model="guestdocument"),
        codename="document_reveal").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("guests", "0002_alter_guest_consent_version_alter_guest_created_at_and_more"),
    ]

    operations = [
        migrations.RunPython(create_reveal_permission, remove_reveal_permission),
    ]
