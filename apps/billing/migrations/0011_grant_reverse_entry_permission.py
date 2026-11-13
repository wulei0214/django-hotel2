# -*- coding: utf-8 -*-
"""基线 2.0 §4.1：红字冲销权限码入矩阵并授权。

billing.reverse_entry（挂在 LedgerEntry content type，Meta.permissions 声明，
post_migrate 会同步 Permission 行；此处 get_or_create 保证迁移期即可授权）：

- billing.reverse_entry  红字冲销执行权 → 财务、值班经理、店长
  （沿用 hotelapp.migrations.0014 的冲账授权口径："冲账（红字）执行权归
  财务（新增）+ 值班经理/店长（复核）"；P0-04 F-08：不复活已退役的
  hotelapp.reverse_checkout，新 HTTP 入口以新权限码授权。）

等价性说明：hotelapp.finance_access 是财务中心读取门禁，不含冲销执行权；
二者在 API 层叠加校验（finance_access + reverse_entry）。回滚：reverse
仅移除组授权，权限点保留（与 0008 同口径）。
"""
from django.db import migrations


def grant(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ct, _ = ContentType.objects.get_or_create(app_label="billing", model="ledgerentry")
    perm, _ = Permission.objects.get_or_create(
        content_type=ct,
        codename="reverse_entry",
        defaults={"name": "可以红字冲销账本凭证（LedgerEntry）"},
    )
    for name in ("财务", "值班经理", "店长"):
        group = Group.objects.filter(name=name).first()
        if group:
            group.permissions.add(perm)


def revoke(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    try:
        perm = Permission.objects.get(
            content_type__app_label="billing",
            content_type__model="ledgerentry",
            codename="reverse_entry",
        )
    except Permission.DoesNotExist:
        return
    for group in Group.objects.all():
        group.permissions.remove(perm)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0010_alter_ledgerentry_options"),
        ("hotelapp", "0014_role_actions"),
    ]
    operations = [migrations.RunPython(grant, revoke)]
