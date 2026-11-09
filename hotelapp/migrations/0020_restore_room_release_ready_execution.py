# -*- coding: utf-8 -*-
"""F-08 补执行点（基线 2.0 §4.3 清洁与维修工作台）：净房放行授权面恢复。

P0-04 曾以"权限点存在但无执行点"为由将 hotelapp.room_release_ready 从注册表
SPECIAL_PERMISSIONS 授权面移除（缺口登记册 F-08）。§4.3 清洁与维修工作台为该
权限恢复了合法执行点：

    POST /api/v1/housekeeping/rooms/<room_id>/status  target=CLEAN（净房放行）
    → apps/inventory/services.change_housekeeping_status（服务层校验）
    → apps/api/housekeeping_center.py（API 层校验）

本迁移按**现有语义原样**add-only 确保授权面（不回收任何既有授权）：

  * room_release_ready → 值班经理、店长（0014 决策 6：净房放行归当天管理者；
    权限点与历史组授权在 P0-04 期间均未删除，本迁移为声明式确保，幂等）；
  * view_room / change_room → 客房服务员（0014 决策 6：清洁链流转
    待清洁→清洁中→待检查；净房放行不在内，故不授予 room_release_ready）。

等价性说明：授予集 = 0014/0016 既定语义集合，行为无放宽；
reverse 不回滚（幂等 ensure 的授予与历史授予同源，见 revoke 说明）。
"""
from django.db import migrations

# (codename, 挂靠 hotelapp 模型, 中文名) —— 与 0014 ACTION_PERMS 一致
ROOM_RELEASE_PERM = ("room_release_ready", "room", "能净房放行 客房信息")

# 专项权限 → 授予的中文角色组（决策 6：值班经理/店长）
RELEASE_GRANT_GROUPS = ("值班经理", "店长")

# 清洁链流转（待清洁→清洁中→待检查、检查退回）→ 客房服务员
HOUSEKEEPING_CHAIN_PERMS = (("view_room", "room"), ("change_room", "room"))
HOUSEKEEPING_CHAIN_GROUP = ("客房服务员",)


def _ensure_perm(Permission, ContentType, model, codename, name):
    ct, _ = ContentType.objects.get_or_create(
        app_label="hotelapp", model=model)
    perm, _ = Permission.objects.get_or_create(
        content_type=ct, codename=codename, defaults={"name": name})
    return perm


def grant(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")

    codename, model, name = ROOM_RELEASE_PERM
    perm = _ensure_perm(Permission, ContentType, model, codename, name)
    for group_name in RELEASE_GRANT_GROUPS:
        group = Group.objects.filter(name=group_name).first()
        if group:
            group.permissions.add(perm)

    for codename, model in HOUSEKEEPING_CHAIN_PERMS:
        chain_perm = Permission.objects.filter(
            content_type__app_label="hotelapp", codename=codename).first()
        if chain_perm is None:
            chain_perm = _ensure_perm(
                Permission, ContentType, model, codename, f"能管理 {model}")
        for group_name in HOUSEKEEPING_CHAIN_GROUP:
            group = Group.objects.filter(name=group_name).first()
            if group:
                group.permissions.add(chain_perm)


def revoke(apps, schema_editor):
    """不回滚（与 0016 同口径）：本迁移是幂等 ensure，授予集与 0014/0016 既定
    语义完全一致——回滚移除会误伤历史授予（0014 创建、P0-04 期间保留），
    破坏"授权面与注册表一致"的目标状态。权限点本体亦不删除。"""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("hotelapp", "0019_export_approve_perm"),
    ]

    operations = [
        migrations.RunPython(grant, revoke),
    ]
