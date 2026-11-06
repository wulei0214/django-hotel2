"""hotel URL 路由表。

路由分组：
  /login/ /logout/          前台独立登录/退出
  /                         按当前角色权限跳转到首个可访问模块
  /check_unique/            唯一字段失焦查重接口（JSON）
  /index/ /insert/ ...      员工管理（index 即员工模块首页）
  /show_client/ ...         其余 7 个模块：show_/insert_/update_/delete_/search_ 五件套
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
"""
"""hotel URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/2.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView
from hotelapp import views
from apps.identity import invitation_views
from apps.identity import permission_admin
from apps.identity import permission_center
from apps.identity import platform as identity_platform
from apps.identity import staff_center_views
from apps.api import pages as api_pages
from apps.api import openapi_schema

urlpatterns = [
    path('api/v1/', include('apps.api.urls')),
    path('api/docs/', openapi_schema.swagger_ui, name='pms2_swagger_ui'),
    path('api/redoc/', openapi_schema.redoc_ui, name='pms2_redoc_ui'),
    path('reserve/', api_pages.public_booking_page, name='public_booking_page'),
    # ---- 员工中心（§10.1：员工生命周期写工作台；PMS 会话域） ----
    # 必须先于 manage/<center>/ 通用注册表路由匹配。
    path('manage/staff/', staff_center_views.staff_center_page, name='staff_center'),
    path('manage/staff/actions/create/', staff_center_views.staff_action_create, name='staff_center_create'),
    path('manage/staff/actions/<int:pk>/provision/', staff_center_views.staff_action_provision, name='staff_center_provision'),
    path('manage/staff/actions/<int:pk>/assign_role/', staff_center_views.staff_action_assign_role, name='staff_center_assign_role'),
    path('manage/staff/actions/<int:pk>/end_assignment/<int:assignment_id>/', staff_center_views.staff_action_end_assignment, name='staff_center_end_assignment'),
    path('manage/staff/actions/<int:pk>/set_status/', staff_center_views.staff_action_set_status, name='staff_center_set_status'),
    path('manage/staff/actions/<int:pk>/reset_password/', staff_center_views.staff_action_reset_password, name='staff_center_reset_password'),
    # ---- 邀请激活（AUTH-17/18，方案 §5.2/§5.3）：员工中心邀请操作 + 匿名激活页 ----
    path('manage/staff/actions/<int:pk>/invite/revoke/', staff_center_views.staff_action_invite_revoke, name='staff_center_invite_revoke'),
    path('manage/staff/actions/<int:pk>/invite/resend/', staff_center_views.staff_action_invite_resend, name='staff_center_invite_resend'),
    path('manage/staff/actions/<int:pk>/invite/', staff_center_views.staff_action_invite, name='staff_center_invite'),
    path('invite/accept/', invitation_views.invite_accept, name='invite_accept'),
    path('manage/<str:center>/', api_pages.management_center_page, name='pms_management_center'),
    path('admin/', admin.site.urls),
    path('', views.home),
    path('home/', views.home),
    path('login/', views.login_view),
    path('force-password-change/', views.force_password_change),
    path('register/', views.register_view),
    path('logout/', views.logout_view),
    path('booking/', views.booking_page),
    path('checkin/', views.checkin_workspace),
    path('checkin/legacy/', views.checkin_manage),
    path('checkin_confirm/', views.checkin_confirm),
    path('checkin_in/', views.checkin_in),
    path('cancel_booking/', views.cancel_booking),
    path('checkout/', views.checkout),
    path('uncheckout/', views.uncheckout),
    path('extend_stay/', views.extend_stay),
    path('change_room/', views.change_room),
    path('express_checkout/', views.express_checkout),
    path('walkin_create/', views.walkin_create),
    path('no_show_one/', views.no_show_one),
    path('folio/', views.folio_api),
    path('nightaudit/', views.night_audit),
    path('nightaudit_run/', views.night_audit_run),
    path('room_status/', views.room_status_change),
    path('finance/', views.finance_manage),
    path('finance_add/', views.finance_add),
    path('check_unique/', views.check_unique),
    path('client/reveal_id/', views.client_reveal_id),
    path('vip/reveal_tel/', views.vip_reveal_tel),
    path('staff_accounts/', views.staff_accounts, name='staff_accounts'),
    path('staff_accounts/<int:pk>/', views.staff_accounts_detail, name='staff_accounts_detail'),
    # ---- §10.1 步骤 7：旧写入口退役，只保留 302 兼容跳转 → /manage/staff/ ----
    path('staff_accounts/create/', RedirectView.as_view(url='/manage/staff/', permanent=False), name='staff_accounts_create'),
    path('staff_accounts/<int:pk>/provision/', RedirectView.as_view(url='/manage/staff/', permanent=False), name='staff_accounts_provision'),
    path('staff_accounts/<int:pk>/assign_role/', RedirectView.as_view(url='/manage/staff/', permanent=False), name='staff_accounts_assign_role'),
    path('staff_accounts/<int:pk>/end_assignment/<int:assignment_id>/', RedirectView.as_view(url='/manage/staff/', permanent=False), name='staff_accounts_end_assignment'),
    path('staff_accounts/<int:pk>/set_status/', RedirectView.as_view(url='/manage/staff/', permanent=False), name='staff_accounts_set_status'),
    path('staff_accounts/<int:pk>/reset_password/', RedirectView.as_view(url='/manage/staff/', permanent=False), name='staff_accounts_reset_password'),
    # ---- 平台身份域（§6.1）：/platform/ 平台管理面，平台会话域双 Path Cookie ----
    path('platform/', identity_platform.platform_home, name='platform_home'),
    # 平台门户登出（POST）：flush 平台会话 → 中间件删除两份平台 Cookie + legacy 清理
    path('platform/logout/', identity_platform.platform_logout, name='platform_logout'),
    path('platform/permissions/', permission_admin.permission_admin_page, name='platform_permissions'),
    path('platform/permissions/save/', permission_admin.permission_admin_save, name='platform_permissions_save'),
    # ---- 权限管理中心 API（§8.1-8.3）：读取/保存/高危确认 token ----
    path('platform/api/permissions/<int:group_id>/',
         permission_center.permission_read, name='platform_permission_read'),
    path('platform/api/permissions/<int:group_id>/save/',
         permission_center.permission_save, name='platform_permission_save'),
    path('platform/api/permissions/<int:group_id>/confirm-token/',
         permission_center.permission_confirm_token,
         name='platform_permission_confirm_token'),
    path('platform/system/', identity_platform.platform_system_info, name='platform_system'),
    # 旧 /permissions/ 入口 302 兼容跳转（矩阵页已迁移至 /platform/permissions/）
    path('permissions/', RedirectView.as_view(url='/platform/permissions/', permanent=False)),
    path('permissions/save/', RedirectView.as_view(url='/platform/permissions/save/', permanent=False)),
    # PMS 员工个人页（仅本人信息 + 自助改密；平台环境信息在 /platform/system/）
    path('system/', views.system_info),
    path('staff_toggle/', views.staff_toggle),
    path('export/staff/', views.export_staff),
    path('export/staff/request/', views.request_staff_export),
    path('export/staff/approve/<int:approval_id>/', views.approve_staff_export),
    path('export/<str:module>/', views.export_module),
    path('index/', views.index),
    path('insert/', views.insert),
    path('insert_client/', views.insert_client),
    path('delete_staff/', views.delete_staff),
    path('search_staff/', views.search_staff),
    #path('single/', views.single),
    path('update_staff/', views.update_staff),
    path('show_client/', views.show_client),
    path('delete_client/', views.delete_client),
    path('update_client/', views.update_client),
    path('search_client/', views.search_client),
    path('insert_food/', views.insert_food),
    path('show_food/', views.show_food),
    path('delete_food/', views.delete_food),
    path('update_food/', views.update_food),
    path('search_food/', views.search_food),
    path('insert_vip/', views.insert_vip),
    path('show_vip/', views.show_vip),
    path('delete_vip/', views.delete_vip),
    path('update_vip/', views.update_vip),
    path('search_vip/', views.search_vip),
    path('insert_park/', views.insert_park),
    path('show_park/', views.show_park),
    path('delete_park/', views.delete_park),
    path('update_park/', views.update_park),
    path('search_park/', views.search_park),
    #path('show_cook/', views.show_cook),
    path('insert_room/', views.insert_room),
    path('show_room/', views.show_room),  # 旧入口先执行原权限校验，再跳转 PMS2 房态与库存中心
    path('delete_room/', views.delete_room),
    path('update_room/', views.update_room),
    path('search_room/', views.search_room),
    path('insert_order/', views.insert_order),
    path('show_order/', views.show_order),
    path('delete_order/', views.delete_order),
    path('search_order/', views.search_order),
    path('insert_accomodation/', views.insert_accomodation),
    path('delete_accomodation/', views.delete_accomodation),
    path('search_accomodation/', views.search_accomodation),
    path('show_accomodation/', views.show_accomodation),
]