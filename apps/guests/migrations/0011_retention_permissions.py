"""T02-3：隐私清理三项专项权限（OWNER 2026-11-24 裁定方案 B）。

request = guests.retention_request（店长）；
approve = guests.retention_approve（值班经理/店长）；
execute = guests.retention_execute（值班经理/店长）。
权限点本体创建由 guests 域迁移承接；角色授权同步由 identity 域迁移承接。
"""

from django.db import migrations

PERMISSIONS = (
    ("retention_request", "能申请 隐私数据保留期满清理"),
    ("retention_approve", "能审批 隐私数据保留期满清理"),
    ("retention_execute", "能执行 已批准的隐私数据清理"),
)


def create_retention_permissions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct, _ = ContentType.objects.get_or_create(app_label="guests", model="guest")
    for codename, name in PERMISSIONS:
        Permission.objects.get_or_create(
            content_type=ct, codename=codename, defaults={"name": name}
        )


def remove_retention_permissions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission.objects.filter(
        content_type=ContentType.objects.get(app_label="guests", model="guest"),
        codename__in=[item[0] for item in PERMISSIONS],
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("guests", "0010_guest_name_encryption_stage1")]

    operations = [
        migrations.RunPython(create_retention_permissions, remove_retention_permissions),
    ]
