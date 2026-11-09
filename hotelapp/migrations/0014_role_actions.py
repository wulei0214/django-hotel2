# -*- coding: utf-8 -*-
"""RBAC 改造阶段 1：统一角色目录（9 业务角色）+ 高危动作权限点。

对应《PMS_RBAC_权限管理改造方案_评审稿》与用户确认的 9 项决策：
  1. 采用 9 个业务角色；收益/房价经理、审计只读暂缓但预留扩展位
  2. 冲账（红字）执行权归 财务（新增）+ 值班经理/店长（复核）；无金额阈值（单店）
  4. 值班经理授权场景以独立权限点落地（反结账/未到/净房放行），可扩展
  5. 夜审由值班经理/店长兼岗执行（夜审员组预留，0 权限待人员到位）
  6. 客房岗位不拆分清洁/检查：客房服务员可流转至「待检查」，净房放行归管理者
  7. 餐饮/停车模块代码与数据保留、页面入口隐藏（对接第三方后启用）

高危动作权限点（自定义 Permission，挂在 hotelapp 各模型 CT 上）：
  room_release_ready    净房放行（待检查→空闲）      → 值班经理、店长
  night_audit_execute   执行夜审                      → 值班经理、店长
  business_date_reopen  重开营业日（预留，暂无视图）   → 值班经理、店长
  reverse_checkout      反结账（已退房→已入住）        → 值班经理、店长
  no_show_handle        未到订单处理                  → 值班经理、店长
  finance_access        财务管理入口                  → 财务、值班经理、店长
  export_pii            敏感数据导出（身份证/薪酬/联系方式）→ 财务、值班经理、店长
"""
from django.db import migrations

# (codename, 中文名, 挂靠模型, 说明)
ACTION_PERMS = [
    ("room_release_ready", "能净房放行 客房信息", "room",
     "净房放行：待检查→空闲，由当天管理者（值班经理/店长）确认"),
    ("night_audit_execute", "能执行夜审", "nightaudit",
     "执行营业日结：未到处理、房费过租、生成日结记录"),
    ("business_date_reopen", "能重开营业日", "nightaudit",
     "夜审后重开营业日（预留权限点，视图实现后启用）"),
    ("reverse_checkout", "能反结账 客房订单", "accommodation",
     "反结账：已退房→已入住，冲销对应收款流水"),
    ("no_show_handle", "能处理未到订单", "accommodation",
     "单个标记未到（No-Show）并释放房间"),
    ("finance_access", "能访问财务管理", "financerecord",
     "财务页入口：流水、收支与报表"),
    ("export_pii", "能导出敏感数据", "client",
     "导出含身份证号/薪酬/联系方式的数据（员工、顾客、会员 CSV）"),
]

# 预留业务角色（组已建、0 权限，后续版本在权限矩阵页按需勾选）
RESERVED_ROLES = ["夜审员", "餐饮服务员", "餐饮主管", "停车安保"]

# 客房服务员：清洁链流转（待清洁→清洁中→待检查）；净房放行不在内
HOUSEKEEPING_PERMS = ["view_room", "change_room"]

# 冲账执行权：财务（原只读）补 2 项；店长补 2 项（值班经理 0005 已有）
# 决策 7-2：员工月薪本人/财务/店长可见 —— 财务补员工目录只读权限
FINANCE_REVERSE_PERMS = ["delete_order", "delete_accommodation", "view_staff"]

# 高危动作 → 授予的中文角色组
ACTION_GRANTS = {
    "room_release_ready": ["值班经理", "店长"],
    "night_audit_execute": ["值班经理", "店长"],
    "business_date_reopen": ["值班经理", "店长"],
    "reverse_checkout": ["值班经理", "店长"],
    "no_show_handle": ["值班经理", "店长"],
    "finance_access": ["财务", "值班经理", "店长"],
    "export_pii": ["财务", "值班经理", "店长"],
}


def create_role_actions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")

    def ct(model):
        # 测试库迁移时 post_migrate 尚未生成 ContentType 行，必须 get_or_create
        ct_obj, _ = ContentType.objects.get_or_create(
            app_label="hotelapp", model=model)
        return ct_obj

    # 1. 高危动作权限点
    for codename, name, model, _desc in ACTION_PERMS:
        Permission.objects.get_or_create(
            codename=codename, content_type=ct(model), defaults={"name": name}
        )

    # 2. 新角色组（客房服务员带初始权限，其余预留空组）
    hk, _ = Group.objects.get_or_create(name="客房服务员")
    hk.permissions.set(list(Permission.objects.filter(
        content_type__app_label="hotelapp", codename__in=HOUSEKEEPING_PERMS)))
    for role in RESERVED_ROLES:
        Group.objects.get_or_create(name=role)

    # 3. 高危动作授权 + 财务/店长冲账执行权
    for codename, roles in ACTION_GRANTS.items():
        perm = Permission.objects.get(codename=codename, content_type__app_label="hotelapp")
        for role in roles:
            Group.objects.get(name=role).permissions.add(perm)
    perm_ids = list(Permission.objects.filter(
        content_type__app_label="hotelapp", codename__in=FINANCE_REVERSE_PERMS))
    for role in ("财务", "店长"):
        Group.objects.get(name=role).permissions.add(*perm_ids)

    # 4. 清理双轨英文组（P0：两套角色目录）；仅删除无用户、无权限的空组
    for g in Group.objects.filter(name__in=[
            "FRONT_DESK", "DUTY_MANAGER", "FINANCE", "PROPERTY_OWNER",
            "HOUSEKEEPING", "NIGHT_AUDIT"]):
        if g.user_set.count() == 0 and g.permissions.count() == 0:
            g.delete()


def remove_role_actions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")

    for codename, _n, model, _d in ACTION_PERMS:
        Permission.objects.filter(
            codename=codename,
            content_type=ContentType.objects.get(app_label="hotelapp", model=model),
        ).delete()
    for role in RESERVED_ROLES + ["客房服务员"]:
        Group.objects.filter(name=role).delete()
    # 撤销授予（组若已删则跳过）
    added = [p for p, _r in ACTION_GRANTS.items()]
    Permission.objects.filter(codename__in=added).delete()
    for role in ("财务", "店长"):
        g = Group.objects.filter(name=role).first()
        if g:
            g.permissions.remove(*Permission.objects.filter(
                content_type__app_label="hotelapp",
                codename__in=FINANCE_REVERSE_PERMS))


class Migration(migrations.Migration):

    dependencies = [
        ("hotelapp", "0013_alter_accommodation_入住时间_alter_accommodation_冲账人_and_more"),
    ]

    operations = [
        migrations.RunPython(create_role_actions, remove_role_actions),
    ]
