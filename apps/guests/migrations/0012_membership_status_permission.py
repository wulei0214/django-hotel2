"""NS-E-05 IMPLEMENT-001 / MB-11：会员暂停/恢复专项权限点。

OWNER 2026-11-24 逐项裁定 MB-11 = APPROVED：
从 hotelapp.change_vip 拆出 guests.membership_status_manage，
change_vip 仅保留等级变更、失去暂停/恢复能力（权限收紧）。

本迁移只创建权限点本体；角色授权由 identity 域迁移承接
（与 guests/0011 retention 权限同一分工）。
"""

from django.db import migrations

PERMISSION = (
    "membership_status_manage",
    "能暂停和恢复 会员关系（NS-E-05 MB-11）",
)


def create_membership_status_permission(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct, _ = ContentType.objects.get_or_create(app_label="guests", model="guestmembership")
    codename, name = PERMISSION
    Permission.objects.get_or_create(content_type=ct, codename=codename, defaults={"name": name})


def remove_membership_status_permission(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct = ContentType.objects.filter(app_label="guests", model="guestmembership").first()
    if ct is None:
        return
    Permission.objects.filter(content_type=ct, codename=PERMISSION[0]).delete()


class Migration(migrations.Migration):
    dependencies = [("guests", "0011_retention_permissions")]

    operations = [
        migrations.RunPython(
            create_membership_status_permission,
            remove_membership_status_permission,
        ),
    ]
