# Phase 0 审计 10 —— 全量路由与入口清单（工作流 B）

- 审计基线：`main` @ `100a93a1382ca310aae588bc3a1734e4d67e94ba`（`git rev-parse HEAD` 已核对一致；工作树仅未跟踪的 `docs/audit/`）。
- 枚举方法：Django URL resolver 全量遍历（`get_resolver('hotel.urls')`，含 `apps.api.urls` 与 `admin.site.urls`）+ 逐视图源码装饰器分析；`DJANGO_DB_ENGINE=sqlite` 只读环境，`manage.py check` 通过（0 issues）。
- 证据标记：`urls.py:行号` = hotel/urls.py；`api/urls.py:行号` = apps/api/urls.py；视图行号 = 对应 views 文件中装饰器/定义行。
- 本报告不含任何凭据、token、会话键或完整手机号/证件号。

## 0. 身份域判定依据

| 身份域 | Cookie | 依据 |
|---|---|---|
| PMS 员工 | `sessionid` | `apps/identity/session_middleware.py:36-39`（SplitSessionMiddleware：仅 `/admin/`、`/permissions/` 用 admin 命名空间）；`hotel/settings.py:159-160`（ADMIN_SESSION_COOKIE_NAME） |
| 平台（Django Admin + 权限矩阵页） | `admin_sessionid` | `apps/identity/session_middleware.py:21-29`（is_admin_request 覆盖 `/admin/` 与 `/permissions/`） |
| 匿名公开 | 无会话 | 各公开视图无登录门禁，见下表逐条证据 |

员工会话之外的通用上下文：`apps/identity/middleware.py:34-69` PropertyContextMiddleware 为员工解析 DEFAULT 物业任职（`request.staff_membership`），无任职则授权必然失败。

统计总览：全 resolver 共 **337** 条 URL 模式 = Django Admin 191 + `/api/v1/` 60 + 其他 86（其中 hotelapp 82 + PMS2 页面/文档 4）。

---

## A. 顾客端公开面（匿名可达，13 条）

| # | 路径 | 视图 | 方法 | 权限/凭据要求 | 状态 | 证据 |
|---|---|---|---|---|---|---|
| A1 | `/reserve/` | apps.api.pages.public_booking_page | GET（无限制） | 匿名；无物业时渲染 503 | ACTIVE | urls.py:40；pages.py:13-33 |
| A2 | `/api/v1/availability` | api.views.availability | GET | 匿名；限流未见（公开查询面） | ACTIVE | api/urls.py:35；apps/api/views.py:66-68 |
| A3 | `/api/v1/reservations` | api.views.reservations_create | POST | 匿名；限流 IP 10/10min + phone_hash 3/10min（fail-closed）；幂等键 | ACTIVE | api/urls.py:36；views.py:153-158（rate_limit.check） |
| A4 | `/api/v1/reservations/<booking_no>` | api.views.reservation_detail | GET | 匿名 + **BookingAccessToken**（X-Booking-Token / ?token=）+ 限流 public.reservation_query | ACTIVE | api/urls.py:37；views.py:233-248 |
| A5 | `/api/v1/reservations/<booking_no>/cancel` | api.views.reservation_cancel | POST | 匿名 + **BookingAccessToken** + 限流 public.reservation_cancel | ACTIVE | api/urls.py:38；views.py:389-411 |
| A6 | `/api/v1/payments/intents` | api.views.payment_intent_create | POST | 匿名 + **BookingAccessToken** + 仅 dev/test 环境（生产 403）+ 限流 public.payment_intent | ACTIVE | api/urls.py:39；views.py:289-296,300-310 |
| A7 | `/api/v1/payments/callback` | api.views.payment_callback | POST | **csrf_exempt**；仅 DEBUG+dev/test（生产 404）；X-Signature HMAC 验签 | ACTIVE（dev-only） | api/urls.py:40；views.py:366-378（csrf_exempt、verify_mock_callback） |
| A8 | `/api/v1/openapi.json` | api.views.openapi_document | GET | 匿名 | ACTIVE | api/urls.py:41；views.py:435-440 |
| A9 | `/api/docs/` | openapi_schema.swagger_ui | GET | 匿名（静态 HTML，指向 openapi.json） | ACTIVE | urls.py:38；openapi_schema.py:219-222 |
| A10 | `/api/redoc/` | openapi_schema.redoc_ui | GET | 匿名 | ACTIVE | urls.py:39；openapi_schema.py:224-227 |
| A11 | `/login/` | hotelapp.views.login_view | GET+POST | 匿名；登录限流 auth.login 5 次/5 分钟（fail-closed）；有 DEFAULT 任职者 302→/home/ | ACTIVE | urls.py:45；hotelapp/views.py:1902-1943 |
| A12 | `/register/` | hotelapp.views.register_view | 任意 | 匿名；整函数 302→`/reserve/`（AUTH-01 止血：不再创建 User） | REDIRECT | urls.py:47；views.py:1945-1953 |
| A13 | `/booking/` | hotelapp.views.booking_page | 任意 | 匿名；整函数 302→`/reserve/`（AUTH-02 旧预订入口冻结） | REDIRECT | urls.py:49；views.py:1996-2004 |

---

## B. PMS 员工页面（sessionid 域，28 条）

门禁体系：`@staff_required`（登录 + is_active + DEFAULT 有效任职，hotelapp/views.py:108-130）+ `@pms_permission_required('<app>.<codename>')`（AuthorizationService 物业范围检查，apps/identity/decorators.py:21-45）。

| # | 路径 | 视图 | 方法 | 权限要求 | 状态 | 证据 |
|---|---|---|---|---|---|---|
| B1 | `/` 与 `/home/` | hotelapp.views.home | GET | staff_required；财务卡另查 `hotelapp.finance_access` / `identity.staff_manage`（展示级） | ACTIVE | urls.py:43-44；views.py:1961-1994 |
| B2 | `/force-password-change/` | views.force_password_change | GET+POST | staff_required；无 must_change_password 标记则 302→/home/ | ACTIVE | urls.py:46；views.py:1798-1830 |
| B3 | `/logout/` | views.logout_view | 任意 | `@login_required`；登出后 302→/login/ | ACTIVE | urls.py:48；views.py:1955-1959 |
| B4 | `/cancel_booking/` | views.cancel_booking | 任意 | staff_required（残留）；整函数 302→`/reserve/` | REDIRECT | urls.py:54；views.py:2033-2039 |
| B5 | `/checkin/` | views.checkin_workspace | GET | staff_required + `hotelapp.view_accommodation` | ACTIVE | urls.py:50；views.py:2006-2014 |
| B6 | `/checkin/legacy/` | views.checkin_manage | GET | staff_required + `hotelapp.view_accommodation`；反结账按钮按 `hotelapp.reverse_checkout` 展示 | ACTIVE | urls.py:51；views.py:2016-2031 |
| B7 | `/no_show_one/` | views.no_show_one | POST | staff_required + 函数内 `hotelapp.no_show_handle`（403） | ACTIVE | urls.py:61；views.py:1358-1377 |
| B8 | `/folio/` | views.folio_api | GET | staff_required + 函数内 `hotelapp.view_accommodation`（PermissionDenied） | ACTIVE | urls.py:62；views.py:1380-1414 |
| B9 | `/nightaudit/` | views.night_audit | GET | staff_required + 函数内 `hotelapp.night_audit_execute` | ACTIVE | urls.py:63；views.py:2261-2273 |
| B10 | `/finance/` | views.finance_manage | GET | staff_required + 函数内 `hotelapp.finance_access` | ACTIVE | urls.py:66；views.py:2211-2218 |
| B11 | `/check_unique/` | views.check_unique | GET | 仅 staff_required（无业务权限码；唯一性查重接口，低敏） | ACTIVE | urls.py:68；views.py:194-208 |
| B12 | `/client/reveal_id/` | views.client_reveal_id | POST | staff_required + `hotelapp.guest_document_reveal` + 原因≥8字符 + AuditEvent | ACTIVE | urls.py:69；views.py:1508-1533 |
| B13 | `/vip/reveal_tel/` | views.vip_reveal_tel | POST | staff_required + `hotelapp.guest_phone_reveal` + 原因 + AuditEvent | ACTIVE | urls.py:70；views.py:1535-1560 |
| B14-B21 | `/staff_accounts/`、`/create/`、`/<pk>/`、`/<pk>/provision/`、`/assign_role/`、`/end_assignment/<id>/`、`/set_status/`、`/reset_password/` | views.staff_accounts_* | GET/POST | staff_required + `identity.staff_manage`（全部 8 条统一装饰） | ACTIVE | urls.py:71-78；views.py:1588-1797 |
| B22 | `/system/` | views.system_info | GET | staff_required（个人/环境信息，无业务权限码） | ACTIVE | urls.py:80；views.py:2286-2310 |
| B23 | `/export/staff/request/` | views.request_staff_export | POST | staff_required + 函数内 `hotelapp.export_pii`；写 ApprovalRecord（privacy.export_staff） | ACTIVE | urls.py:83；views.py:279-314 |
| B24 | `/export/staff/approve/<id>/` | views.approve_staff_export | POST | staff_required + **`_has_role(user,'值班经理','店长')` 硬编码角色**（非权限点） | ACTIVE | urls.py:84；views.py:315-356 |
| B25 | `/export/<module>/` | views.export_module | GET | staff_required + `_has_role('店长','财务')` + `hotelapp.view_<module>`；身份证/电话列强制脱敏 | ACTIVE | urls.py:85；views.py:1459-1505 |
| B26 | `/manage/<center>/` | api.pages.management_center_page | GET | `@login_required` + 函数内按 center 查 `MANAGEMENT_CENTERS[center]["permission"]`（staff→identity.staff_manage；guests→view_client；rooms→view_room；memberships→view_vip；reservations→view_accommodation）；未知 center 404 | ACTIVE | urls.py:41；apps/api/pages.py:56-58,168-176 |
| B27 | `/index/` | views.index | GET | staff_required + `hotelapp.view_staff`（员工管理首页） | ACTIVE | urls.py:87；views.py:232-250 |

（B26 属 PMS2 页面但挂在根 URLconf，计入本组；总数按 28 计含 `/`、`/home/` 两条。）

---

## C. 平台管理面（admin_sessionid 域，193 条）

| # | 路径 | 视图 | 方法 | 权限要求 | 状态 | 证据 |
|---|---|---|---|---|---|---|
| C1 | `/permissions/` | apps.identity.permission_admin.permission_admin_page | GET | `@user_passes_test(_is_superuser)`（仅 superuser）；session cookie 为 admin_sessionid | ACTIVE | urls.py:79；permission_admin.py:74-88 |
| C2 | `/permissions/save/` | permission_admin.permission_admin_save | POST | 同上（superuser）；保存以页面白名单 MODULES/ACTION_PERMS 为准，整体覆盖角色 hotelapp 权限集 | ACTIVE | urls.py:86；permission_admin.py:129+ |
| C3 | `/admin/` 下 191 条模式 | django.contrib.admin（登录/退出/首页 + 21 个注册模型的 list/add/change/delete/history） | GET/POST | Django Admin 认证（admin_sessionid）+ is_staff + 模型权限；非 superuser 已被 D-04 回收 is_staff（identity/migrations/0004） | ACTIVE | urls.py:42；settings.py:57-67；hotelapp/admin.py:14-141；apps/{core,identity,billing,inventory,reservations,guests}/admin.py |

Admin 注册模型清单（21 个）：hotelapp 8（Accommodation/Client/Room/Staff/Vip/FinanceRecord/NightAudit/Folio，全部 NoAdminDeleteMixin，Order 状态只读；Food/Park/Order 未注册 = 直接 404，hotelapp/admin.py:134-141）、core 4（Property/AuditEvent/LegacyRecordLink/BreakGlassRecord）、identity 3（StaffProfile/Membership/RoleAssignment，只读 Admin，apps/identity/admin.py:9-38）、inventory 5（RoomType/Room/RatePlan/DailyRate/RoomTypeInventory）、billing 5（Folio/LedgerEntry/Payment/Refund/ApprovalRecord）、reservations 2（Reservation/Stay）、guests 1（Guest）。

---

## D. PMS2 API —— 员工面（sessionid 域，53 条，均在 `/api/v1/` 下）

认证/授权机制：`_front_desk_required`/`_front_desk_write_required`/`_front_desk_action_required`（apps/api/front_desk.py:58-95,758-776）；`_context(request, permission)`（apps/api/night_audit.py:16-27）；`@api_view` 异常封套（apps/core/api_contract.py:78-91，本身**不含**认证）。未认证统一 401 JSON，无权限统一 403 JSON。

### D1. 夜审 / 营业日（7 条）

| 路径 | 方法 | 权限码 | 状态 | 证据 |
|---|---|---|---|---|
| `night-audit/preview` | GET | `hotelapp.night_audit_execute` | ACTIVE | api/urls.py:11；night_audit.py:35-46 |
| `night-audit/run` | POST | `hotelapp.night_audit_execute` | ACTIVE | api/urls.py:12；night_audit.py:49-63 |
| `night-audit/business-dates/<id>/reopen-request` | POST | `hotelapp.business_date_reopen` | ACTIVE | api/urls.py:13；night_audit.py:66-82 |
| `night-audit/business-dates/<id>/approvals/<aid>/approve` | POST | `hotelapp.business_date_reopen` | ACTIVE | api/urls.py:14；night_audit.py:85-101 |
| `night-audit/business-dates/<id>/break-glass` | POST | `hotelapp.business_date_reopen`（另二次密码验证） | ACTIVE | api/urls.py:15；night_audit.py:104-126 |
| `night-audit/business-dates/<id>/break-glass/<rid>/execute` | POST | `hotelapp.business_date_reopen` | ACTIVE | api/urls.py:16；night_audit.py:129-148 |
| `night-audit/break-glass/<rid>/review` | POST | `core.breakglass_review` | ACTIVE | api/urls.py:17；night_audit.py:151-175 |

### D2. 前台工作台（22 条）

| 路径 | 方法 | 权限 | 状态 | 证据 |
|---|---|---|---|---|
| `front-desk/overview`、`arrivals`、`departures`、`in-house`、`reservations`、`reservations/<booking_no>`、`rooms`、`rooms/assignable`、`folios/<booking_no>`、`legacy-reservations`、`legacy-reservations/<id>`、`stays/<id>/available-rooms`（12 条查询） | GET | `hotelapp.view_accommodation`（@_front_desk_required） | ACTIVE | api/urls.py:19-30；front_desk.py:58-71 |
| `stays/<id>/extend`、`stays/<id>/change-room`、`reservation-rooms/<id>/assign`、`reservation-rooms/<id>/check-in`、`reservations/<booking_no>/deposits`、`reservations/<booking_no>/payments`、`stays/<id>/check-out`（7 条写） | POST | `view_accommodation` **且** `change_accommodation`（@_front_desk_write_required） | ACTIVE | api/urls.py:31-43；front_desk.py:82-93 |
| `reservation-rooms/<id>/no-show` | POST | `hotelapp.no_show_handle`（@_front_desk_action_required） | ACTIVE | api/urls.py:33；front_desk.py:966-984 |
| `payments/<id>/refunds` | POST | `billing.refund_request` | ACTIVE | api/urls.py:34；front_desk.py:1005-1025 |
| `refunds/<id>/approve` | POST | `billing.refund_approve` | ACTIVE | api/urls.py:35；front_desk.py:1028-1048 |

### D3. Guest 中心 / 房态 / 财务汇总（10 条）

| 路径 | 方法 | 权限 | 状态 | 证据 |
|---|---|---|---|---|
| `guests`、`guests/<id>` | GET | `hotelapp.view_client` | ACTIVE | api/urls.py:47-48；guest_center.py:92-99,119-125 |
| `guests/create` | POST | `hotelapp.add_client` | ACTIVE | api/urls.py:49；guest_center.py:283-290 |
| `guests/<id>/update` | POST | `hotelapp.change_client` | ACTIVE | api/urls.py:50；guest_center.py:308-314 |
| `guests/<id>/documents/<did>/reveal` | POST | `hotelapp.guest_document_reveal` | ACTIVE | api/urls.py:51；guest_center.py:333-340 |
| `guests/<id>/phones/<pid>/reveal` | POST | `hotelapp.guest_phone_reveal` | ACTIVE | api/urls.py:52；guest_center.py:366-373 |
| `rooms/status`、`room-types`、`rate-calendar` | GET | `hotelapp.view_room` | ACTIVE | api/urls.py:55-57；guest_center.py:158-223 |
| `financial-summary` | GET | `hotelapp.finance_access`（明细可见性同码复核） | ACTIVE | api/urls.py:58；guest_center.py:425-445 |

### D4. 审批 / 预订管理 / 审计 / 房价 / 员工 / 会员（14 条）

| 路径 | 方法 | 权限 | 状态 | 证据 |
|---|---|---|---|---|
| `approvals` | GET | 无权限码——有 DEFAULT 任职即可查本物业审批列表 | ACTIVE | api/urls.py:60；approval.py:29-48 |
| `approvals/request` | POST | **无权限码**——有任职即可发起（action_code 自由传入；SoD 靠审批侧把关） | ACTIVE | api/urls.py:61；approval.py:51-83 |
| `approvals/<id>/approve`、`/reject` | POST | `billing.refund_approve`（_check_approver，注释明确审批不依赖 finance_access） | ACTIVE | api/urls.py:62-63；approval.py:17-25,86-116 |
| `reservations-mgmt`、`reservations-mgmt/<booking_no>` | GET | `hotelapp.view_accommodation` | ACTIVE | api/urls.py:65-66；reservation_center.py:12-24 |
| `audit-timeline` | GET | `identity.staff_manage` | ACTIVE | api/urls.py:68；reservation_center.py:82-95 |
| `daily-rates/<id>/adjust` | POST | `inventory.manage_rates`（⚠ OpenAPI 文档误标为 `hotelapp.change_room`，见 11-permissions.md 缺陷 P-C-3） | ACTIVE | api/urls.py:69；guest_center.py:396-403 |
| `staff`、`staff/<id>` | GET | `identity.staff_manage` | ACTIVE | api/urls.py:71-72；staff_center.py:75-105 |
| `memberships`、`memberships/<id>` | GET | `hotelapp.view_vip` | ACTIVE | api/urls.py:74-75；staff_center.py:120-170 |
| `memberships/create` | POST | `hotelapp.add_vip` | ACTIVE | api/urls.py:76；staff_center.py:200-208 |
| `memberships/<id>/level` | POST | `hotelapp.change_vip` | ACTIVE | api/urls.py:77；staff_center.py:239-247 |

---

## E. 旧 hotelapp 写路由（RETIRED-410 清单，35 条）

统一实现：函数体首行 `return HttpResponse("该旧写入口已退役（410 Gone），请使用 PMS2 工作台。", status=410)`（模板见 hotelapp/views.py:255-256）；原装饰器（staff_required + 权限码）保留在前，410 硬返回在其后，故这些路由**先过认证/权限、再吃 410**，不存在未授权探测差异。业务能力已由 PMS2 API 接管（§D）。

| # | 路径 | 原权限装饰 | 方法 | 证据（views.py） |
|---|---|---|---|---|
| E1 | `/checkin_confirm/` | change_accommodation | POST | urls.py:52；views.py:1243-1249 |
| E2 | `/checkin_in/` | change_accommodation | POST | urls.py:53；views.py（同模式） |
| E3 | `/checkout/` | change_accommodation | POST | urls.py:55；views.py:2111-2116 |
| E4 | `/uncheckout/` | （装饰后 410；函数内 reverse_checkout 检查已成死代码） | POST | urls.py:56；views.py:2138-2146 |
| E5 | `/extend_stay/` | change_accommodation | POST | urls.py:57；views.py:1243-1248 |
| E6 | `/change_room/` | change_accommodation | POST | urls.py:58；views.py:1262-1268 |
| E7 | `/express_checkout/` | change_accommodation | POST | urls.py:59 |
| E8 | `/walkin_create/` | add_accommodation | POST | urls.py:60 |
| E9 | `/nightaudit_run/` | staff_required（旧夜审执行退役，附 Location 提示） | 任意 | urls.py:64；views.py:2275-2283 |
| E10 | `/room_status/` | staff_required（room_release_ready 检查死代码） | POST | urls.py:65；views.py:1416-1421 |
| E11 | `/finance_add/` | staff_required（_has_role('财务') 死代码） | POST | urls.py:67；views.py:2221-2225 |
| E12 | `/staff_toggle/` | change_staff | POST | urls.py:81；views.py:252-257 |
| E13 | `/export/staff/` | staff_required（旧薪酬 CSV 通道关闭） | 任意 | urls.py:82；views.py:268-276 |
| E14 | `/insert/` | add_staff | POST | urls.py:88；views.py:358-363 |
| E15 | `/insert_client/` | add_client | POST | urls.py:89；views.py:504-509 |
| E16 | `/delete_staff/` | delete_staff | GET+POST | urls.py:90；views.py:409-414 |
| E17 | `/update_staff/` | change_staff | POST | urls.py:93；views.py:441-446 |
| E18 | `/delete_client/` | delete_client | GET+POST | urls.py:95；views.py:537-542 |
| E19 | `/update_client/` | change_client | POST | urls.py:96；views.py:552-557 |
| E20 | `/insert_food/` | add_food + module_gate(food) | POST | urls.py:98；views.py:602-608 |
| E21 | `/delete_food/` | delete_food + module_gate(food) | GET+POST | urls.py:100；views.py:638-644 |
| E22 | `/update_food/` | change_food + module_gate(food) | POST | urls.py:101；views.py:654-660 |
| E23 | `/insert_vip/` | add_vip | POST | urls.py:103；views.py:705-710 |
| E24 | `/delete_vip/` | delete_vip | GET+POST | urls.py:105；views.py:740-745 |
| E25 | `/update_vip/` | change_vip | POST | urls.py:106；views.py:755-760 |
| E26 | `/insert_park/` | add_park + module_gate(park) | POST | urls.py:108；views.py:805-811 |
| E27 | `/delete_park/` | delete_park + module_gate(park) | GET+POST | urls.py:110；views.py:838-844 |
| E28 | `/update_park/` | change_park + module_gate(park) | POST | urls.py:111；views.py:854-860 |
| E29 | `/insert_room/` | add_room | POST | urls.py:114；views.py:910-915 |
| E30 | `/delete_room/` | delete_room | GET+POST | urls.py:116；views.py:943-948 |
| E31 | `/update_room/` | change_room | POST | urls.py:117；views.py:958-963 |
| E32 | `/insert_order/` | add_order + module_gate(order) | POST | urls.py:119；views.py:1013-1019 |
| E33 | `/delete_order/` | delete_order + module_gate(order) | GET+POST | urls.py:121；views.py:1059-1065 |
| E34 | `/insert_accomodation/` | add_accommodation | POST | urls.py:123；views.py:1114-1119 |
| E35 | `/delete_accomodation/` | delete_accommodation | POST | urls.py:124；views.py:1162-1167 |

---

## F. 旧只读路由（ACTIVE，15 条）

show_*/search_* 五件套中的读侧 + 员工搜索；均 staff_required + 对应 view_* 权限；food/order/park 三模块叠加 module_gate（MODULE_REGISTRY 关闭时 302→/home/，fail-closed）。

| 路径 | 权限 | 附加门禁 | 证据（views.py） |
|---|---|---|---|
| `/search_staff/` | hotelapp.view_staff | — | 424-429 |
| `/show_client/`、`/search_client/` | hotelapp.view_client | — | 495-500, 579-584 |
| `/show_food/`、`/search_food/` | hotelapp.view_food | module_gate("food") | 592-598, 682-688 |
| `/show_vip/`、`/search_vip/` | hotelapp.view_vip | — | 696-701, 782-787 |
| `/show_park/`、`/search_park/` | hotelapp.view_park | module_gate("park") | 795-801, 883-889 |
| `/show_room/`、`/search_room/` | hotelapp.view_room | — | 897-902, 990-995 |
| `/show_order/`、`/search_order/`（POST 查询） | hotelapp.view_order | module_gate("order") | 1003-1009, 1086-1092 |
| `/show_accomodation/`、`/search_accomodation/`（POST 查询） | hotelapp.view_accommodation | — | 1105-1110, 1219-1224 |

---

## G. 导出 / 其他

导出入口已在 §B23-B25（新双人审批通道）与 §E13（旧通道 410）覆盖；无其他独立导出路由。旧 `single/`、`show_cook/` 路由已被注释禁用（urls.py:92,113），不构成入口。

---

## 总数统计与风险标注

**总数：337**
| 分组 | 条数 |
|---|---|
| A 顾客端公开面 | 13 |
| B PMS 员工页面 | 28 |
| C 平台管理面（/permissions/ 2 + /admin/ 191） | 193 |
| D PMS2 API 员工面 | 53 |
| E 旧写路由 410 | 35 |
| F 旧只读路由 | 15 |
| **合计** | **337** |

风险标注（按优先级）：

1. **[中] `/api/v1/payments/callback` 是全站唯一 csrf_exempt 端点**（apps/api/views.py:366）。当前有三层缓解：仅 `DEBUG && DJANGO_ENV∈{dev,test}` 时可达（否则 404）、HMAC 签名验证（X-Signature）、mock 支付仅在 dev 开放（AUTH-07/P0-12）。残留风险集中在"生产误开 DEBUG"场景，建议 Phase 1 直接移除该路由或加环境硬开关。
2. **[中] 匿名预订三件套（A3/A5/A6）依赖限流 + BookingAccessToken**：创建接口凭 phone 提交、取消/查询/支付凭 token，限流 key 覆盖 IP 与 booking/phone_hash（views.py:158,300-310,392-394）。 RateLimitCounter 已落地（apps/core/models.py），但 availability（A2）与 openapi/docs（A8-A10）无限流，可被爬取房型/库存节奏（低敏但建议观测）。
3. **[低] `/approvals/request`（D4）无权限码**：任意有任职的员工可为任意 action_code 发起审批申请（approval.py:51-83）；申请≠审批（审批侧强制 billing.refund_approve），但审批队列可被灌垃圾申请，建议收敛可申请的 action_code 白名单。
4. **[低] `cancel_booking/` 文档与实现漂移**：docstring 称"去掉员工门禁"（views.py:2034-2035），实际仍挂 `@staff_required`（views.py:2033）——匿名旧书签用户会被引到员工登录页而非 /reserve/，行为不一致但无安全影响。
5. **[信息] 410 路由共 35 条**，全部保留原认证+权限装饰（先门禁后 410），无未授权探测差异；`uncheckout/`、`room_status/`、`finance_add/` 函数体内残留权限检查已成死代码（见 11-permissions.md 的执行点判定）。
6. **[信息] `/api/v1/` 员工接口未使用 csrf_exempt**（D 组 53 条全部默认 CSRF 保护，POST 走 CsrfViewMiddleware；CSRF 失败对 /api/ 返回 JSON，settings.py:71-72），无豁免清单。
