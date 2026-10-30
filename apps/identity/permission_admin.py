"""角色权限矩阵管理（超管专属）。

设计要点（对应 PMS 2.0 说明书 §5.4.1 岗位权限矩阵）：
  * 以「角色」为分配对象：页面编辑的是所选角色的权限模板，超管拥有全部权限且不受此页控制
  * 权限矩阵：行 = 业务模块，列 = 查看/新增/修改/删除，单元格为勾选框
  * 「查看」同时决定该模块在侧边栏菜单中的可见性
  * 保存时整体覆盖该角色的 hotelapp 权限集合，并以本页注册的权限点为白名单防伪造提权
"""
import logging

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.contrib.auth.models import Group, Permission
from django.shortcuts import redirect, render

audit = logging.getLogger("hotel.audit")

# 业务模块注册表：key / 中文名 / Django model 名（用于权限点 codename）
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

# 动作列（全中文）：查看同时决定菜单可见性
ACTIONS = [("view", "查看"), ("add", "新增"), ("change", "修改"), ("delete", "删除")]

# 全部合法权限点（保存白名单）
ALL_CODENAMES = {f"{action}_{m['model']}" for m in MODULES for action, _ in ACTIONS}

# 可编辑的业务角色
ROLE_GROUPS = ["前台", "值班经理", "财务", "店长"]


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
            "actions": [{"action": a, "label": lbl,
                         "checked": f"{a}_{m['model']}" in owned}
                        for a, lbl in ACTIONS],
        })

    return render(request, "permissions.html", {
        "roles": roles,
        "group": group,
        "modules": modules,
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
