# -*- coding: utf-8 -*-
"""RBAC 关联方案 Phase 2（D-04）+ 员工工作台权限。

1. 新权限 identity.staff_manage（员工与账号管理工作台，授予店长）：
   工作台是店长日常人事入口（列表/建档/开账号/分角色/停用离职），
   Django Admin 的 identity 模型页不对店长开放（D-03）。
2. D-04：is_staff 仅表示 Django Admin 站点访问，不再作为 PMS 员工门禁——
   回收全部非 superuser 的 is_staff（演示/测试/绑定账号统一处理）。
   staff_required 门禁已同步改为"登录 + DEFAULT 有效任职 + 有效角色授权"。
"""
from django.db import migrations


def create_staff_manage_perm(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")

    ct, _ = ContentType.objects.get_or_create(app_label="identity", model="staffprofile")
    Permission.objects.get_or_create(
        content_type=ct, codename="staff_manage", defaults={"name": "能管理 员工与账号"}
    )
    perm = Permission.objects.get(content_type=ct, codename="staff_manage")
    owner = Group.objects.filter(name="店长").first()
    if owner is not None:
        owner.permissions.add(perm)


def remove_staff_manage_perm(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission.objects.filter(
        content_type=ContentType.objects.get(app_label="identity", model="staffprofile"),
        codename="staff_manage",
    ).delete()


def revoke_is_staff_from_business_accounts(apps, schema_editor):
    """D-04：非 superuser 一律回收 is_staff（PMS 登录不再依赖它）。"""
    User = apps.get_model("auth", "User")
    User.objects.filter(is_staff=True, is_superuser=False).update(is_staff=False)


def restore_is_staff(apps, schema_editor):
    """反向不恢复（历史账号此前本不应持有 admin 入口）。"""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("identity", "0003_alter_propertystaffmembership_created_at_and_more"),
    ]

    operations = [
        migrations.RunPython(create_staff_manage_perm, remove_staff_manage_perm),
        migrations.RunPython(revoke_is_staff_from_business_accounts, restore_is_staff),
    ]
