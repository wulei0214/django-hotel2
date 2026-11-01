# -*- coding: utf-8 -*-
"""RBAC V2 第一阶段（P0）数据迁移：统一员工授权体系 + 老库兼容 + 演示账号安全。

对应第二轮评审 P0-1/P0-3/P0-4：

1. 演示账号身份链（仅 dev/test）：
   评审 P0-1 —— bind_staff_user 创建的账号此前只有 StaffRoleAssignment（新身份体系），
   没有 User.groups / is_staff（旧后台体系），无法登录旧后台。
   统一方案：**授权载体收敛为同一套中文 Django Group**——
   StaffRoleAssignment 保留为"授权记录"（物业任职+有效期+授权人），
   User.groups 同步镜像该组作为运行时权限载体，两条路径读同一组，不再有两套目录。
   本迁移为演示账号补齐 StaffProfile → Membership(DEFAULT) → RoleAssignment →
   User.groups 的完整链（dev/test 环境才执行；见第 3 点）。

2. 老数据库权限补齐（评审 P0-4）：
   0005 曾在 ContentType 缺失时静默跳过权限授予，且已执行过的迁移不会重跑；
   本迁移按"默认模板"对九个角色组 **add-only 补齐**缺失权限（不回收，保留用户自定义）。

3. 演示账号环境门禁（评审 P0-3）：
   DJANGO_ENV=prod 时不创建演示账号，并禁用已存在的固定密码演示账号（is_active=False）；
   dev/test 环境保持可用（本地开发便利），但身份链完整、可审计。

4. 英文角色组用户迁移（评审 P0-4）：
   0015 只删除空组；对仍有用户的英文组，先把用户转入映射的中文组，再移除旧关系并删组。
"""
import os
from datetime import datetime

from django.db import migrations
from django.utils import timezone

# ---------- 与 0005/0014 一致的默认权限模板（add-only 快照；修改规则需同步三处） ----------
ROLE_PERMS = {
    "前台": [("client", "view_client"), ("client", "add_client"), ("client", "change_client"),
            ("vip", "view_vip"), ("vip", "add_vip"), ("vip", "change_vip"),
            ("order", "view_order"), ("order", "add_order"), ("order", "change_order"),
            ("accommodation", "view_accommodation"), ("accommodation", "add_accommodation"),
            ("accommodation", "change_accommodation"),
            ("park", "view_park"), ("park", "add_park"), ("park", "change_park"),
            ("food", "view_food"), ("room", "view_room")],
    "值班经理": [("client", "view_client"), ("client", "add_client"), ("client", "change_client"),
              ("vip", "view_vip"), ("vip", "add_vip"), ("vip", "change_vip"),
              ("order", "view_order"), ("order", "add_order"), ("order", "change_order"),
              ("accommodation", "view_accommodation"), ("accommodation", "add_accommodation"),
              ("accommodation", "change_accommodation"),
              ("park", "view_park"), ("park", "add_park"), ("park", "change_park"),
              ("food", "view_food"), ("room", "view_room"), ("room", "change_room"),
              ("order", "delete_order"), ("accommodation", "delete_accommodation")],
    "财务": [("client", "view_client"), ("vip", "view_vip"), ("order", "view_order"),
            ("accommodation", "view_accommodation"), ("food", "view_food"),
            ("room", "view_room"), ("park", "view_park")],
    "店长": [("staff", "view_staff"), ("staff", "add_staff"), ("staff", "change_staff"),
           ("client", "view_client"), ("client", "add_client"), ("client", "change_client"),
           ("food", "view_food"), ("food", "add_food"), ("food", "change_food"),
           ("vip", "view_vip"), ("vip", "add_vip"), ("vip", "change_vip"),
           ("park", "view_park"), ("park", "add_park"), ("park", "change_park"),
           ("room", "view_room"), ("room", "add_room"), ("room", "change_room"),
           ("order", "view_order"), ("order", "add_order"), ("order", "change_order"),
           ("accommodation", "view_accommodation"), ("accommodation", "add_accommodation"),
           ("accommodation", "change_accommodation")],
}

# 0014 之后追加的高危/调整权限（按角色）
EXTRA_PERMS = {
    "值班经理": ["room_release_ready", "night_audit_execute", "business_date_reopen",
              "reverse_checkout", "no_show_handle", "finance_access", "export_pii"],
    "店长": ["room_release_ready", "night_audit_execute", "business_date_reopen",
           "reverse_checkout", "no_show_handle", "finance_access", "export_pii",
           "delete_order", "delete_accommodation", "view_staff"],
    "财务": ["finance_access", "export_pii", "delete_order", "delete_accommodation",
           "view_staff"],
    "客房服务员": ["view_room", "change_room"],
}

# 英文组 → 中文组映射（与 bind_staff_user.ROLE_CODE_TO_GROUP 一致）
ENGLISH_GROUP_MAP = {
    "FRONT_DESK": "前台",
    "DUTY_MANAGER": "值班经理",
    "FINANCE": "财务",
    "PROPERTY_OWNER": "店长",
    "HOUSEKEEPING": "客房服务员",
    "NIGHT_AUDIT": "夜审员",
}

# 演示账号 → (密码, 角色, 工号)
DEMO_USERS = {
    "front": ("front123456", "前台", "F-0001"),
    "manager": ("manager123456", "值班经理", "F-0002"),
    "finance": ("finance123456", "财务", "F-0003"),
    "owner": ("owner123456", "店长", "F-0004"),
}


def _is_prod():
    return os.environ.get("DJANGO_ENV", "dev").strip().lower() == "prod"


def _ensure_perm(Permission, ContentType, model, codename):
    ct, _ = ContentType.objects.get_or_create(app_label="hotelapp", model=model)
    perm, _ = Permission.objects.get_or_create(
        content_type=ct, codename=codename,
        defaults={"name": f"能管理 {model}"})
    return perm


def _backfill_group_permissions(apps):
    """对默认模板做 add-only 补齐（不回收，保留用户在矩阵页的自定义）。"""
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")

    for role, grants in ROLE_PERMS.items():
        group = Group.objects.filter(name=role).first()
        if group is None:
            continue
        for model, codename in grants:
            group.permissions.add(_ensure_perm(Permission, ContentType, model, codename))
    for role, codenames in EXTRA_PERMS.items():
        group = Group.objects.filter(name=role).first()
        if group is None:
            continue
        for codename in codenames:
            perm = Permission.objects.filter(
                content_type__app_label="hotelapp", codename=codename).first()
            if perm is not None:
                group.permissions.add(perm)


def _migrate_english_group_users(apps):
    """英文组用户 → 中文组；随后移除英文组（双轨目录最终清理）。"""
    Group = apps.get_model("auth", "Group")
    for eng_name, cn_name in ENGLISH_GROUP_MAP.items():
        eng = Group.objects.filter(name=eng_name).first()
        if eng is None:
            continue
        cn = Group.objects.filter(name=cn_name).first()
        if cn is None:
            cn = Group.objects.create(name=cn_name)
        for user in eng.user_set.all():
            user.groups.add(cn)
            user.groups.remove(eng)
        if eng.user_set.count() == 0:
            eng.delete()


def _build_demo_identity_chain(apps):
    """为演示账号补齐 StaffProfile → Membership(DEFAULT) → RoleAssignment → User.groups。"""
    StaffProfile = apps.get_model("identity", "StaffProfile")
    Membership = apps.get_model("identity", "PropertyStaffMembership")
    Assignment = apps.get_model("identity", "StaffRoleAssignment")
    Property = apps.get_model("core", "Property")
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("auth", "User")

    property_obj = Property.objects.filter(status="ACTIVE").order_by("id").first()
    if property_obj is None:
        return
    for username, (password, role, employee_no) in DEMO_USERS.items():
        user = User.objects.filter(username=username).first()
        if user is None:
            user = User.objects.create_user(username=username, password=password)
        # D-04：is_staff 仅 Django Admin；演示账号不持有（0017 统一回收）
        user.is_active = True
        user.save()
        group = Group.objects.get(name=role)
        user.groups.add(group)

        profile, created = StaffProfile.objects.get_or_create(user=user)
        if created or not profile.full_name:
            profile.full_name = username
            profile.employment_status = "ACTIVE"
            profile.save()
        membership, _ = Membership.objects.get_or_create(
            property=property_obj, staff_profile=profile,
            defaults={"employee_no": employee_no,
                      "position_code": role, "status": "ACTIVE"})
        Assignment.objects.get_or_create(
            membership=membership, group=group,
            defaults={"starts_at": timezone.now(), "granted_by": None})


def disable_demo_accounts_in_prod(apps):
    """prod 环境：禁用固定密码演示账号（保留数据引用，不物理删除）。"""
    User = apps.get_model("auth", "User")
    for username in DEMO_USERS:
        User.objects.filter(username=username).update(is_active=False)


def apply_unification(apps, schema_editor):
    # 顺序：权限补齐 → 英文组用户迁移 → 演示账号处理
    _backfill_group_permissions(apps)
    _migrate_english_group_users(apps)
    if _is_prod():
        # 生产：决不自动创建固定密码业务账号，并禁用历史演示账号
        disable_demo_accounts_in_prod(apps)
    else:
        _build_demo_identity_chain(apps)


def reverse_unification(apps, schema_editor):
    """不可逆数据迁移：权限补齐/身份链/账号迁移不做回滚（保留审计与现状）。"""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("hotelapp", "0015_remove_english_groups"),
        ("identity", "0003_alter_propertystaffmembership_created_at_and_more"),
        ("core", "0002_alter_property_address_alter_property_code_and_more"),
    ]

    operations = [
        migrations.RunPython(apply_unification, reverse_unification),
    ]
