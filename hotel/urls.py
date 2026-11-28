"""hotel URL 路由表。

路由分组：
  /login/ /logout/          前台独立登录/退出
  /                         按当前角色权限跳转到首个可访问模块
  /manage/<center>/ ...     PMS2 管理中心注册表（客人/会员/房态/预订 等 11 个中心）
  /index/ /show_client/ ... 旧只读入口 → 302 跳对应 /manage/<center>/（ARCH-02 REDIRECT，
                            门禁不变；/insert_/delete_/update_ 旧写入口仍为 410）
  /manage/staff/ ...        员工中心（§10.1：生命周期写工作台 + actions 写路由）
  /invite/accept/           员工邀请激活页（AUTH-18：匿名公开 + 限流 + 一次性 token）
  /staff_accounts/ ...      旧员工工作台：列表/详情保留只读，写路由 302 → /manage/staff/
  /admin/                   Django 后台（simpleui 主题，超管用）
  /platform/ ...            平台管理面（§6.1：首页/权限矩阵/环境信息；平台会话域
                            platform_sessionid @ /platform/ + django_admin_sessionid @ /admin/，
                            同一平台 Session，见 session_middleware §B.1）
  /platform/logout/         平台门户登出（POST：flush 平台会话 → 双 Cookie 同步删除）
  /platform/api/permissions/...  权限管理中心读写 API（§8，JSON + 内容 hash 并发保护）
  /permissions/             旧矩阵入口 → 302 兼容跳转 /platform/permissions/
  /system/                  PMS 员工个人页（仅本人信息 + 自助改密）

所有业务路由均受登录 + 模块权限保护，见 hotelapp/views.py 各视图装饰器。

（原 Django 2.1 脚手架 URL Configuration 注释已并入本 docstring。）
"""

from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView
from hotelapp import views

from apps.api import openapi_schema
from apps.api import pages as api_pages
from apps.core.admin_views import pms_business_overview
from apps.identity import invitation_views, permission_admin, permission_center, staff_center_views
from apps.identity import platform as identity_platform
from apps.identity.decorators import pms_permission_required


def _legacy_center_redirect(target, permission_code, target_permission_code=None):
    """ARCH-02 REDIRECT：旧只读入口退役为 302 跳转 PMS2 新管理中心。

    退役的是旧页渲染能力，不是访问控制：门禁口径保持不变
    （staff_required + 原权限码）——无有效任职/无权限仍 403，匿名仍走登录墙。
    永久化（301）需业务确认，本轮统一 302。

    target_permission_code：目标中心的门禁权限码。给出时额外要求该权限，
    以守住「旧入口放行 ⇒ 新中心放行」不变式：
    否则会出现「旧入口 302 → 新中心 403」的死胡同——用户被引导到一个自己
    根本无权进入的页面（2026-11-26 复核实证：/index/ 与 /search_staff/ 的
    门禁是 hotelapp.view_staff，而 /manage/staff/ 要求 identity.staff_manage，
    财务岗位持有前者却不持有后者）。此情形下在旧入口直接 403，不做误导性跳转。
    """
    view = RedirectView.as_view(url=target, permanent=False)
    if target_permission_code:
        view = pms_permission_required(target_permission_code)(view)
    return views.staff_required(pms_permission_required(permission_code)(view))


urlpatterns = [
    path("api/v1/", include("apps.api.urls")),
    path("api/docs/", openapi_schema.swagger_ui, name="pms2_swagger_ui"),
    path("api/redoc/", openapi_schema.redoc_ui, name="pms2_redoc_ui"),
    path("reserve/", api_pages.public_booking_page, name="public_booking_page"),
    # ---- 员工中心（§10.1：员工生命周期写工作台；PMS 会话域） ----
    # 必须先于 manage/<center>/ 通用注册表路由匹配。
    path("manage/staff/", staff_center_views.staff_center_page, name="staff_center"),
    path(
        "manage/staff/actions/create/",
        staff_center_views.staff_action_create,
        name="staff_center_create",
    ),
    path(
        "manage/staff/actions/<int:pk>/provision/",
        staff_center_views.staff_action_provision,
        name="staff_center_provision",
    ),
    path(
        "manage/staff/actions/<int:pk>/assign_role/",
        staff_center_views.staff_action_assign_role,
        name="staff_center_assign_role",
    ),
    path(
        "manage/staff/actions/<int:pk>/end_assignment/<int:assignment_id>/",
        staff_center_views.staff_action_end_assignment,
        name="staff_center_end_assignment",
    ),
    path(
        "manage/staff/actions/<int:pk>/set_status/",
        staff_center_views.staff_action_set_status,
        name="staff_center_set_status",
    ),
    path(
        "manage/staff/actions/<int:pk>/rehire/",
        staff_center_views.staff_action_rehire,
        name="staff_center_rehire",
    ),
    path(
        "manage/staff/actions/<int:pk>/update_profile/",
        staff_center_views.staff_action_update_profile,
        name="staff_center_update_profile",
    ),
    path(
        "manage/staff/actions/<int:pk>/reset_password/",
        staff_center_views.staff_action_reset_password,
        name="staff_center_reset_password",
    ),
    # ---- 邀请激活（AUTH-17/18，方案 §5.2/§5.3）：员工中心邀请操作 + 匿名激活页 ----
    path(
        "manage/staff/actions/<int:pk>/invite/revoke/",
        staff_center_views.staff_action_invite_revoke,
        name="staff_center_invite_revoke",
    ),
    path(
        "manage/staff/actions/<int:pk>/invite/resend/",
        staff_center_views.staff_action_invite_resend,
        name="staff_center_invite_resend",
    ),
    path(
        "manage/staff/actions/<int:pk>/invite/",
        staff_center_views.staff_action_invite,
        name="staff_center_invite",
    ),
    path("invite/accept/", invitation_views.invite_accept, name="invite_accept"),
    path("manage/<str:center>/", api_pages.management_center_page, name="pms_management_center"),
    path("admin/login/", identity_platform.platform_admin_login, name="platform_admin_login"),
    # 后台侧边栏的 PMS2 只读业务概览（须在 admin.site.urls 之前匹配）
    path("admin/pms-overview/", pms_business_overview, name="pms_admin_overview"),
    path("admin/", admin.site.urls),
    path("", views.website_home),
    path("home/", views.home),
    path("login/", views.login_view),
    path("force-password-change/", views.force_password_change),
    path("register/", views.register_view),
    path("logout/", views.logout_view),
    path("booking/", views.booking_page),
    path("checkin/", views.checkin_workspace),
    path("checkin/legacy/", _legacy_center_redirect("/checkin/", "hotelapp.view_accommodation")),
    path("checkin_confirm/", views.checkin_confirm),
    path("checkin_in/", views.checkin_in),
    path("cancel_booking/", views.cancel_booking),
    path("checkout/", views.checkout),
    path("uncheckout/", views.uncheckout),
    path("extend_stay/", views.extend_stay),
    path("express_checkout/", views.express_checkout),
    path("walkin_create/", views.walkin_create),
    path("no_show_one/", views.no_show_one),
    path("folio/", views.folio_api),
    path("nightaudit/", views.night_audit),
    path("nightaudit_run/", views.night_audit_run),
    path(
        "finance/",
        RedirectView.as_view(url="/manage/finance/", permanent=False),
        name="legacy_finance_redirect",
    ),
    path("finance_add/", views.finance_add),
    path("client/reveal_id/", views.client_reveal_id),
    path("vip/reveal_tel/", views.vip_reveal_tel),
    path("staff_accounts/", views.staff_accounts, name="staff_accounts"),
    path("staff_accounts/<int:pk>/", views.staff_accounts_detail, name="staff_accounts_detail"),
    # ---- §10.1 步骤 7：旧写入口退役，只保留 302 兼容跳转 → /manage/staff/ ----
    path(
        "staff_accounts/create/",
        RedirectView.as_view(url="/manage/staff/", permanent=False),
        name="staff_accounts_create",
    ),
    path(
        "staff_accounts/<int:pk>/provision/",
        RedirectView.as_view(url="/manage/staff/", permanent=False),
        name="staff_accounts_provision",
    ),
    path(
        "staff_accounts/<int:pk>/assign_role/",
        RedirectView.as_view(url="/manage/staff/", permanent=False),
        name="staff_accounts_assign_role",
    ),
    path(
        "staff_accounts/<int:pk>/end_assignment/<int:assignment_id>/",
        RedirectView.as_view(url="/manage/staff/", permanent=False),
        name="staff_accounts_end_assignment",
    ),
    path(
        "staff_accounts/<int:pk>/set_status/",
        RedirectView.as_view(url="/manage/staff/", permanent=False),
        name="staff_accounts_set_status",
    ),
    path(
        "staff_accounts/<int:pk>/reset_password/",
        RedirectView.as_view(url="/manage/staff/", permanent=False),
        name="staff_accounts_reset_password",
    ),
    # ---- 平台身份域（§6.1）：/platform/ 平台管理面，平台会话域双 Path Cookie ----
    path("platform/", identity_platform.platform_home, name="platform_home"),
    # 平台门户登出（POST）：flush 平台会话 → 中间件删除两份平台 Cookie + legacy 清理
    path("platform/logout/", identity_platform.platform_logout, name="platform_logout"),
    path(
        "platform/permissions/", permission_admin.permission_admin_page, name="platform_permissions"
    ),
    path(
        "platform/permissions/save/",
        permission_admin.permission_admin_save,
        name="platform_permissions_save",
    ),
    # ---- 权限管理中心 API（§8.1-8.3）：读取/保存/高危确认 token ----
    path(
        "platform/api/permissions/<int:group_id>/",
        permission_center.permission_read,
        name="platform_permission_read",
    ),
    path(
        "platform/api/permissions/<int:group_id>/save/",
        permission_center.permission_save,
        name="platform_permission_save",
    ),
    path(
        "platform/api/permissions/<int:group_id>/confirm-token/",
        permission_center.permission_confirm_token,
        name="platform_permission_confirm_token",
    ),
    # ---- 迭代-1 变更 A（治理 V1.1 第三阶段，L4 已批）：高危矩阵变更第二人审批 ----
    path(
        "platform/api/permissions/<int:group_id>/approvals/<int:record_id>/approve/",
        permission_center.permission_approval_approve,
        name="platform_permission_approval_approve",
    ),
    path(
        "platform/api/permissions/<int:group_id>/approvals/<int:record_id>/reject/",
        permission_center.permission_approval_reject,
        name="platform_permission_approval_reject",
    ),
    path("platform/system/", identity_platform.platform_system_info, name="platform_system"),
    # 旧 /permissions/ 入口 302 兼容跳转（矩阵页已迁移至 /platform/permissions/）
    path("permissions/", RedirectView.as_view(url="/platform/permissions/", permanent=False)),
    path(
        "permissions/save/",
        RedirectView.as_view(url="/platform/permissions/save/", permanent=False),
    ),
    # PMS 员工个人页（仅本人信息 + 自助改密；平台环境信息在 /platform/system/）
    path("system/", views.system_info),
    path("staff_toggle/", views.staff_toggle),
    path("export/staff/", views.export_staff),
    path("export/staff/request/", views.request_staff_export),
    path("export/staff/approve/<int:approval_id>/", views.approve_staff_export),
    path("export/<str:module>/", views.export_module),
    path(
        "index/",
        _legacy_center_redirect("/manage/staff/", "hotelapp.view_staff", "identity.staff_manage"),
    ),
    path("insert/", views.insert),
    path("insert_client/", views.insert_client),
    path("delete_staff/", views.delete_staff),
    path(
        "search_staff/",
        _legacy_center_redirect("/manage/staff/", "hotelapp.view_staff", "identity.staff_manage"),
    ),
    # path('single/', views.single),
    path("update_staff/", views.update_staff),
    path("show_client/", _legacy_center_redirect("/manage/guests/", "hotelapp.view_client")),
    path("delete_client/", views.delete_client),
    path("update_client/", views.update_client),
    path("search_client/", _legacy_center_redirect("/manage/guests/", "hotelapp.view_client")),
    path("insert_food/", views.insert_food),
    path("show_food/", views.show_food),
    path("delete_food/", views.delete_food),
    path("update_food/", views.update_food),
    path("search_food/", views.search_food),
    path("insert_vip/", views.insert_vip),
    path("show_vip/", _legacy_center_redirect("/manage/memberships/", "hotelapp.view_vip")),
    path("delete_vip/", views.delete_vip),
    path("update_vip/", views.update_vip),
    path("search_vip/", _legacy_center_redirect("/manage/memberships/", "hotelapp.view_vip")),
    path("insert_park/", views.insert_park),
    path("show_park/", views.show_park),
    path("delete_park/", views.delete_park),
    path("update_park/", views.update_park),
    path("search_park/", views.search_park),
    # path('show_cook/', views.show_cook),
    path("show_room/", views.show_room),  # 旧入口先执行原权限校验，再跳转 PMS2 房态与库存中心
    path("search_room/", _legacy_center_redirect("/manage/rooms/", "hotelapp.view_room")),
    path("insert_order/", views.insert_order),
    path("show_order/", views.show_order),
    path("delete_order/", views.delete_order),
    path("search_order/", views.search_order),
    path("insert_accomodation/", views.insert_accomodation),
    path("delete_accomodation/", views.delete_accomodation),
    path(
        "search_accomodation/",
        _legacy_center_redirect("/manage/reservations/", "hotelapp.view_accommodation"),
    ),
    path(
        "show_accomodation/",
        _legacy_center_redirect("/manage/reservations/", "hotelapp.view_accommodation"),
    ),
]
