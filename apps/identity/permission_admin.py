"""角色权限矩阵页（超管专属）——Phase 3 已重构为权限管理中心（§8）。

历史说明（N-1 修复）：
  * 本模块曾自带 MODULES/ACTION_PERMS/ALL_CODENAMES 手工白名单（41 点，
    仅 hotelapp 域，且含 PLANNED 模块 food/order/park 12 点）——伪造 POST
    可借其服务端放行 PLANNED 授权（缺口 N-1）。Phase 3 起该白名单从代码
    路径上整体移除，授权面唯一权威为 apps/identity/registry.py：
      - 页面矩阵结构、可授予集合（allowed）均由注册表派生；
      - 读写走 permission_center.py 的 JSON API（/platform/api/permissions/），
        PLANNED/RETIRED 模块码一律整体 409 拒绝。
  * permission_admin_save 旧 form 保存逻辑已废弃，保留函数仅为旧入口
    /platform/permissions/save/（与 /permissions/save/ 兼容跳转）不 404：
    一律 302 回矩阵页，不再执行任何授权变更。
"""
from django.shortcuts import redirect, render

from .platform import platform_superadmin_required
from .permission_center import page_bootstrap


@platform_superadmin_required
def permission_admin_page(request):
    """权限矩阵主页（§8.1 四区重构）：/platform/permissions/。

    页面骨架 + 注册表 bootstrap（json_script 嵌入），角色数据由前端 JS
    经 GET /platform/api/permissions/<group_id>/ 拉取。
    """
    return render(request, "platform/permissions.html", {
        "nav_active": "permissions",
        "bootstrap_json": page_bootstrap(),
    })


@platform_superadmin_required
def permission_admin_save(request):
    """旧 form 保存入口（已废弃）：兼容 302 跳转回矩阵页，不做任何变更。

    Phase 3 起保存唯一通道为
    POST /platform/api/permissions/<group_id>/save/（JSON + 内容 hash
    并发保护 + 高危二次确认 + 同事务审计）。
    """
    return redirect("/platform/permissions/")
