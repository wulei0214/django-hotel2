"""角色权限矩阵管理（超管专属）。

设计要点（对应《PMS_RBAC_权限管理改造方案_评审稿》+ 用户确认的 9 项决策）：
  * 以「角色」为分配对象：页面编辑的是所选角色的权限模板，超管拥有全部权限且不受此页控制
  * 权限矩阵两个区块：
      - 模块区：行 = 业务模块，列 = 查看/新增/修改/删除；「查看」同时决定菜单可见性
      - 高危动作区：独立权限点（净房放行/执行夜审/反结账/未到处理/财务入口/敏感导出/重开营业日）
  * 隐藏模块（餐饮/停车，对接第三方）行置灰标「预留」，保存时不授予
  * 保存时整体覆盖该角色的 hotelapp 权限集合，并以本页注册的权限点为白名单防伪造提权
  * 角色目录 = 9 个业务角色（含预留空组）+ 2 个占位角色（收益/房价经理、审计只读）
"""
import logging

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.models import Group, Permission
from django.shortcuts import redirect, render

audit = logging.getLogger("hotel.audit")

# 业务模块注册表：key / 中文名 / Django model 名（用于权限点 codename）
# reserved=True 表示模块已隐藏（对接第三方），矩阵行置灰、保存时不授予
MODULES = [
    {"key": "staff", "label": "员工管理", "model": "staff", "reserved": False},
    {"key": "client", "label": "顾客管理", "model": "client", "reserved": False},
    {"key": "food", "label": "餐厅管理", "model": "food", "reserved": True},
    {"key": "room", "label": "客房管理", "model": "room", "reserved": False},
    {"key": "park", "label": "停车场管理", "model": "park", "reserved": True},
    {"key": "vip", "label": "会员信息管理", "model": "vip", "reserved": False},
    {"key": "order", "label": "餐厅订单管理", "model": "order", "reserved": True},
    {"key": "accommodation", "label": "客房订单管理", "model": "accommodation", "reserved": False},
]

# 动作列（全中文）：查看同时决定菜单可见性
ACTIONS = [("view", "查看"), ("add", "新增"), ("change", "修改"), ("delete", "删除")]

# 高危动作权限点（与 hotelapp/migrations/0014 保持一致；修改需三端同步）
ACTION_PERMS = [
    {"code": "room_release_ready", "label": "净房放行（待检查→空闲）",
     "desc": "由当天管理者（值班经理/店长）确认净房"},
    {"code": "night_audit_execute", "label": "执行夜审",
     "desc": "营业日结：未到处理、房费过租、日结记录"},
    {"code": "business_date_reopen", "label": "重开营业日（预留）",
     "desc": "夜审后重开营业日；视图实现后启用，必须值班经理/店长"},
    {"code": "reverse_checkout", "label": "反结账（已退房→已入住）",
     "desc": "冲销对应收款流水；房间已被新客入住或已过夜审则禁止"},
    {"code": "no_show_handle", "label": "未到订单处理",
     "desc": "单个标记 No-Show 并释放房间"},
    {"code": "finance_access", "label": "财务管理入口",
     "desc": "流水、收支与报表（财务录入收支；冲账执行见「删除」列）"},
    {"code": "export_pii", "label": "导出敏感数据",
     "desc": "员工（含月薪）/顾客（身份证）/会员（联系方式）CSV 导出"},
]

# 全部合法权限点（保存白名单）：模块 CRUD + 高危动作
ALL_CODENAMES = ({f"{action}_{m['model']}" for m in MODULES for action, _ in ACTIONS}
                 | {p["code"] for p in ACTION_PERMS})

# 可编辑的业务角色（9 个：5 个现役 + 4 个预留空组；顺序即页面 tab 顺序）
ROLE_GROUPS = ["前台", "值班经理", "财务", "店长", "客房服务员", "夜审员",
               "餐饮服务员", "餐饮主管", "停车安保"]

# 占位角色（暂不建组，页面灰显提示；后续版本启用时迁移建组即可）
RESERVED_ROLE_SLOTS = ["收益/房价经理", "审计只读"]

# 隐藏模块保存时允许保留的权限点：无（模块隐藏 → 权限随之回收，重启用时再授予）


def _is_superuser(user):
    return user.is_authenticated and user.is_superuser


def _role_groups():
    groups = {g.name: g for g in Group.objects.filter(name__in=ROLE_GROUPS)}
    return [(name, groups[name]) for name in ROLE_GROUPS if name in groups]


def _group_codenames(group):
    return set(group.permissions.filter(content_type__app_label="hotelapp")
               .values_list("codename", flat=True))


@user_passes_test(_is_superuser, login_url="/login/")
def permission_admin_page(request):
    """权限矩阵主页：?role=<组id> 选择要编辑的角色，默认第一个"""
    roles = _role_groups()
    if not roles:
        messages.error(request, "尚未初始化业务角色组，请先执行 migrate 初始化。")
        return redirect("/home/")
    selected = request.GET.get("role")
    group = next((g for _, g in roles if str(g.id) == selected), roles[0][1])
    owned = _group_codenames(group)

    modules = []
    for m in MODULES:
        modules.append({
            "key": m["key"],
            "label": m["label"],
            "reserved": m["reserved"],
            "actions": [{"action": a, "label": lbl,
                         "checked": f"{a}_{m['model']}" in owned}
                        for a, lbl in ACTIONS],
        })

    action_perms = []
    for p in ACTION_PERMS:
        action_perms.append({
            "code": p["code"], "label": p["label"], "desc": p["desc"],
            "checked": p["code"] in owned,
        })

    return render(request, "permissions.html", {
        "roles": roles,
        "reserved_roles": RESERVED_ROLE_SLOTS,
        "group": group,
        "actions": ACTIONS,
        "modules": modules,
        "action_perms": action_perms,
        "perm_total": len(owned),
        "perm_all_count": len(ALL_CODENAMES),
    })


@user_passes_test(_is_superuser, login_url="/login/")
def permission_admin_save(request):
    """保存角色权限：原生复选框多值提交，整体覆盖该角色的权限集合"""
    if request.method != "POST":
        return redirect("/permissions/")
    roles = _role_groups()
    group = next((g for _, g in roles if str(g.id) == request.POST.get("role")), None)
    if group is None:
        messages.error(request, "角色不存在或不可编辑")
        return redirect("/permissions/")

    # 原生复选框多值提交：getlist 直接得到每个勾选的权限点，无拼接歧义
    requested = set(request.POST.getlist("perm"))
    granted = requested & ALL_CODENAMES            # 白名单过滤，防伪造提权

    perms = list(Permission.objects.filter(
        content_type__app_label="hotelapp", codename__in=granted))
    group.permissions.set(perms)
    audit.info("权限矩阵保存 role=%s(%s) 授予 %s 项 操作人=%s",
               group.name, group.id, len(granted), request.user.username)
    messages.success(request, f"角色「{group.name}」权限已保存（{len(granted)} 项）")
    return redirect(f"/permissions/?role={group.id}")
