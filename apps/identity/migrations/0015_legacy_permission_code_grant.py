# -*- coding: utf-8 -*-
"""ARCH-04 C10：员工模块权限码解绑旧 ContentType + 既有授权迁移（可逆）。

旧 ``hotelapp.view_staff/add_staff/change_staff/delete_staff`` 绑定在旧
ContentType（hotelapp.staff）上；新员工中心改用新栈权限码
（identity.view_staffprofile 等）。若不把既有授权同步过来，新员工中心 403
（方案 §5.1 M6 高风险点：需保证旧权限迁移有回退路径）。

正向：把「持有旧码的角色组」逐个映射到对应新码；该组已持有新码时不重复授权
（也无动作可撤销，故不登记台账）。每次新增授权写一行台账。
反向：按台账精确撤销——只撤销本迁移新增的授权，不误删迁移前既有授权。
台账（identity.LegacyPermissionCodeGrant）随反向迁移一并删除。

新码是 identity.StaffProfile 模型的 Django 默认 CRUD 权限（模型级权限码，
非新增自定义权限点）；为保证「全新库上迁移期间目标码已存在」，
迁移内以 get_or_create 幂等补齐（post_migrate 的同名创建不会重复）。
"""
from django.db import migrations, models
import django.db.models.deletion

# 旧权限码 → 新栈权限码（1:1，读写语义不变：只读码不映射成可写码）
PERMISSION_CODE_MAP = (
    ("hotelapp.view_staff", "identity.view_staffprofile", "能查看 员工档案"),
    ("hotelapp.add_staff", "identity.add_staffprofile", "能新增 员工档案"),
    ("hotelapp.change_staff", "identity.change_staffprofile", "能修改 员工档案"),
    ("hotelapp.delete_staff", "identity.delete_staffprofile", "能删除 员工档案"),
)

TARGET_CONTENT_TYPE = ("identity", "staffprofile")


def _permission(apps, code, name=None, ensure_content_type=None):
    """按 "app_label.codename" 取权限码。

    ensure_content_type 非空时，权限码不存在则按该模型 ContentType 幂等补齐
    （新码是模型级默认权限；全新库上迁移期间 post_migrate 尚未生成它）。
    """
    app_label, codename = code.split(".", 1)
    Permission = apps.get_model("auth", "Permission")
    perm = Permission.objects.filter(content_type__app_label=app_label, codename=codename).first()
    if perm is not None:
        return perm
    if ensure_content_type is None:
        return None
    ContentType = apps.get_model("contenttypes", "ContentType")
    content_type, _ = ContentType.objects.get_or_create(
        app_label=ensure_content_type[0], model=ensure_content_type[1]
    )
    perm, _ = Permission.objects.get_or_create(
        content_type=content_type,
        codename=codename,
        defaults={"name": name or codename},
    )
    return perm


def rebind_staff_permissions(apps, schema_editor):
    """旧码持有组 → 同步授予对应新码，并登记迁移台账。"""
    Group = apps.get_model("auth", "Group")
    Ledger = apps.get_model("identity", "LegacyPermissionCodeGrant")
    for source_code, target_code, target_name in PERMISSION_CODE_MAP:
        source_perm = _permission(apps, source_code)
        target_perm = _permission(
            apps, target_code, target_name, ensure_content_type=TARGET_CONTENT_TYPE
        )
        if source_perm is None or target_perm is None:
            continue
        for group in Group.objects.filter(permissions=source_perm):
            if group.permissions.filter(pk=target_perm.pk).exists():
                continue
            group.permissions.add(target_perm)
            Ledger.objects.create(group=group, source_code=source_code, target_code=target_code)


def restore_legacy_staff_permissions(apps, schema_editor):
    """按台账精确撤销本迁移新增的授权（不误删迁移前既有授权）。"""
    Ledger = apps.get_model("identity", "LegacyPermissionCodeGrant")
    for row in Ledger.objects.all().order_by("id"):
        target_perm = _permission(apps, row.target_code)
        if target_perm is None:
            continue
        row.group.permissions.remove(target_perm)
    Ledger.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("identity", "0014_staffprofile_legacy_staff_set_null"),
    ]

    operations = [
        migrations.CreateModel(
            name="LegacyPermissionCodeGrant",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("source_code", models.CharField(max_length=64, verbose_name="旧权限码")),
                ("target_code", models.CharField(max_length=64, verbose_name="新权限码")),
                ("granted_at", models.DateTimeField(auto_now_add=True, verbose_name="迁移时间")),
                (
                    "group",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="legacy_permission_grants",
                        to="auth.group",
                        verbose_name="角色组",
                    ),
                ),
            ],
            options={
                "verbose_name": "旧权限码迁移台账",
                "verbose_name_plural": "旧权限码迁移台账",
                "ordering": ("id",),
            },
        ),
        migrations.AddConstraint(
            model_name="legacypermissioncodegrant",
            constraint=models.UniqueConstraint(
                fields=("group", "source_code", "target_code"),
                name="uq_legacy_permission_code_grant",
            ),
        ),
        migrations.RunPython(rebind_staff_permissions, restore_legacy_staff_permissions),
    ]
