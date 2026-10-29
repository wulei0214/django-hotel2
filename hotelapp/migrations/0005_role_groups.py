# -*- coding: utf-8 -*-
"""创建四个业务角色组（前台/值班经理/财务/店长）并分配模块权限，附带演示账号。

权限矩阵（对应酒店行业规范，映射到本系统的模块）：
- 前台：顾客/会员/餐饮订单/客房订单/车位的 查看+新增+修改；菜品/客房 查看。禁止删除与员工管理。
- 值班经理：前台全部 + 客房修改（房价调整）+ 餐饮订单/客房订单删除（反结账/冲账权）。
- 财务：全部业务模块只读（夜审复核、对账）。禁止任何写操作。
- 店长：全部模块 查看+新增+修改（含员工管理）。物理删除不开放给业务角色，仅系统超管可执行。
"""
from django.db import migrations

# 各角色的 (模型, 动作) 权限清单
MODEL_PREFIX = {
    "staff": "staff", "client": "client", "food": "food", "vip": "vip",
    "park": "park", "room": "room", "order": "order", "accommodation": "accommodation",
}

FULL_RO = ["staff", "client", "food", "vip", "park", "room", "order", "accommodation"]
BUSINESS_RW = ["client", "vip", "order", "accommodation", "park"]
BILLING = ["order", "accommodation"]


def _codenames(actions, models):
    out = []
    for m in models:
        for a in actions:
            out.append((m, f"{a}_{m}"))
    return out


ROLE_PERMS = {
    "前台": _codenames(["view", "add", "change"], BUSINESS_RW) + _codenames(["view"], ["food", "room"]),
    "值班经理": _codenames(["view", "add", "change"], BUSINESS_RW)
               + _codenames(["view"], ["food"]) + _codenames(["view", "change"], ["room"])
               + _codenames(["delete"], BILLING),
    "财务": _codenames(["view"], ["client", "vip", "order", "accommodation", "food", "room", "park"]),
    "店长": _codenames(["view", "add", "change"], FULL_RO),
}

DEMO_USERS = {
    "front": ("front123456", "前台"),
    "manager": ("manager123456", "值班经理"),
    "finance": ("finance123456", "财务"),
    "owner": ("owner123456", "店长"),
}


def create_roles(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("auth", "User")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    for role, perm_list in ROLE_PERMS.items():
        group, _ = Group.objects.get_or_create(name=role)
        group.permissions.clear()
        for model_name, codename in perm_list:
            try:
                ct = ContentType.objects.get(app_label="hotelapp", model=model_name)
                perm = Permission.objects.get(content_type=ct, codename=codename)
            except (ContentType.DoesNotExist, Permission.DoesNotExist):
                continue
            group.permissions.add(perm)

    for username, (password, role) in DEMO_USERS.items():
        if User.objects.filter(username=username).exists():
            continue
        user = User.objects.create_user(username=username, password=password)
        user.is_staff = True
        user.save()
        group = Group.objects.get(name=role)
        user.groups.add(group)


def remove_roles(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("auth", "User")
    for role in ROLE_PERMS:
        Group.objects.filter(name=role).delete()
    for username in DEMO_USERS:
        User.objects.filter(username=username).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("hotelapp", "0004_alter_client_身份证号_alter_food_菜名_alter_park_车辆牌号_and_more"),
    ]

    operations = [
        migrations.RunPython(create_roles, remove_roles),
    ]
