"""角色权限分配管理（超管专属，图2 双栏布局）。

设计参考主流 RBAC 管理台（若依 / Mews Role-based Permissions 等）的通用模式：
  * 以「角色」为中心分配权限，而不是逐用户授权；
  * 左侧为菜单可见性树（模块勾选 = view 权限，控制侧边栏入口）；
  * 右侧为各模块的功能权限卡片（查看/新增/修改/删除），支持单卡全选；
  * 保存时整体覆盖该角色的 hotelapp 权限集合，并做护栏校验
    （仅允许本页注册的权限点，防止伪造请求提权）。
"""
import logging

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.models import Group
from django.shortcuts import render, redirect

# 本页可管理的业务模块注册表：key / 展示名 / Django model 的 content_type model 名
MODULES = [
    {"key": "staff", "label": "员工管理", "model": "staff"},
    {"key": "client", "label": "顾客管理", "model": "client"},
    {"key": "food", "label": "餐厅管理", "model": "food"},
    {"key": "room", "label": "客房管理", "model": "room"},
    {"key": "park", "label": "停车场管理", "model": "park"},
    {"key": "vip", "label": "会员信息管理", "model": "vip"},
    {"key": "order", "label": "餐厅订单管理", "model": "order"},
    {"key": "accommodation", "label": "客房订单管理", "model": "accommodation"},
]

# 动作清单：view 同时决定侧边栏菜单可见性
ACTIONS = [("view", "查看"), ("add", "新增"), ("change", "修改"), ("delete", "删除")]

# 全部合法权限点（保存时以此为白名单）
ALL_CODENAMES = {f"{action}_{m['model']}" for m in MODULES for action, _ in ACTIONS}

# 可编辑的业务角色（前台 / 值班经理 / 财务 / 店长）
ROLE_GROUPS = ["前台", "值班经理", "财务", "店长"]

audit = logging.getLogger("hotel.audit")


def _is_superuser(user):
    return user.is_authenticated and user.is_superuser


def _role_groups():
    """按固定顺序返回可编辑角色组（不存在的不展示）"""
    groups = {g.name: g for g in Group.objects.filter(name__in=ROLE_GROUPS)}
    return [(name, groups[name]) for name in ROLE_GROUPS if name in groups]


def _group_perm_codenames(group):
    return set(group.permissions.filter(content_type__app_label="hotelapp")
               .values_list("codename", flat=True))


@user_passes_test(_is_superuser, login_url="/login/")
def permission_admin_page(request):
    """权限分配主页：?role=<id> 选择角色，默认第一个"""
    roles = _role_groups()
    if not roles:
        messages.error(request, "尚未初始化业务角色组，请先执行 migrate 初始化。")
        return redirect("/home/")
    selected = request.GET.get("role")
    group = next((g for _, g in roles if str(g.id) == selected), roles[0][1])
    owned = _group_perm_codenames(group)

    modules = []
    for m in MODULES:
        actions = [{"action": a, "label": lbl,
                    "codename": f"{a}_{m['model']}",
                    "checked": f"{a}_{m['model']}" in owned}
                   for a, lbl in ACTIONS]
        modules.append({**m, "actions": actions,
                        "menu_checked": "view_" + m["model"] in owned})

    ctx = {
        "roles": roles,
        "group": group,
        "modules": modules,
        "perm_total": len(owned),
        "perm_all_count": len(ALL_CODENAMES),
    }
    return render(request, "permissions.html", ctx)


@user_passes_test(_is_superuser, login_url="/login/")
def permission_admin_save(request):
    """保存角色权限：整体覆盖该角色的 hotelapp 权限集合（白名单校验）"""
    if request.method != "POST":
        return redirect("/permissions/")
    roles = _role_groups()
    group = next((g for _, g in roles if str(g.id) == request.POST.get("role")), None)
    if group is None:
        messages.error(request, "角色不存在或不可编辑")
        return redirect("/permissions/")
    requested = set(request.POST.getlist("perm"))
    granted = requested & ALL_CODENAMES            # 白名单过滤，防伪造提权
    dropped = ALL_CODENAMES - granted
    from django.contrib.auth.models import Permission
    perms = list(Permission.objects.filter(
        content_type__app_label="hotelapp", codename__in=granted))
    group.permissions.set(perms)
    audit.info("权限变更 role=%s(%s) 授予=%s 移除=%s 操作人=%s",
               group.name, group.id, sorted(granted - dropped), sorted(dropped),
               request.user.username)
    messages.success(request, f"角色「{group.name}」权限已保存（{len(granted)} 项）")
    return redirect(f"/permissions/?role={group.id}")
