# 30 · Phase 5-B：PII 揭示理由强制 / 模块门禁覆盖 / 旧入口无旁路终验

- 实施依据：《PMS2平台身份权限与业务全链路治理开发实施方案》§10.2 / §10.3 / §10.4
- 实施基线：`main` @ `80b4c58` 起实施（Phase 5-B 测试加固子任务；员工中心迁移由并行任务实施，本次未触碰其领域）；本任务代码变更已随 `main` @ `59e3f9a` 入库
- 测试基线：288 项全过 → 实施完成后全量 **334 项 OK**（288 原有 + Phase 5-B 新增 23 项 + 并行任务新增 23 项）
- 变更文件：
  - `apps/guests/services.py`（最小代码变更：reveal_document 理由强制 + 新增 reveal_phone 服务）
  - `apps/identity/test_module_gates.py`（新增，23 项测试）
  - 本报告（新增）

---

## 1. §10.2 PII 揭示理由强制（实施 + 核实）

### 1.1 服务层（本次代码变更，`apps/guests/services.py`）

| 变更 | 内容 |
|---|---|
| `_require_reveal_reason()`（新增） | 理由 strip 后为空 → `ValueError("揭示必须填写理由")`；非空截断至 240 字符 |
| `reveal_document()` | 权限检查（`hotelapp.guest_document_reveal`，AuthorizationService）之后、解密之前插入理由强制——空白理由**不产生揭示值、不产生审计**。理由检查放在权限检查之后：先授权后参数校验（fail-closed 顺序不回传权限差异信息），同时保持既有调用方语义（无权限者仍得 PermissionDenied） |
| `reveal_phone()`（新增） | 与 reveal_document 同构：`hotelapp.guest_phone_reveal` 独立权限 + 理由强制 + AuditEvent（action=`guest.phone_reveal`，after 仅脱敏值）。此前服务层无电话揭示统一入口，本次补齐（API 层行为不变） |

### 1.2 API 层核实结论（未改代码，仅钉住）

`apps/api/guest_center.py` 的 `guest_reveal_document` / `guest_reveal_phone`（POST）：
- 任务输入问"reason 参数当前可空？"——**核实结果：不可空**。两视图均强制 `8 ≤ len(reason.strip()) ≤ 240`，否则 `ApiError("VALIDATION_ERROR")` → HTTP 400；reason 缺失（无字段）同样落入 400。
- 审计 action：`guest.pii_reveal.document` / `guest.pii_reveal.phone`，`after` 仅 `masked`，`reason` 必含——`apps/api/test_security.py` 已有覆盖，本次补充"缺失/全空白 → 400 且零审计"边界。

### 1.3 旧 UI 揭示入口核实（未改）

`hotelapp/views.py` 的 `client_reveal_id`（`guest.pii_reveal`）与 `vip_reveal_tel`（`member.pii_reveal`）均已强制 8-240 字符理由 + `pms_permission_required` 独立权限码，现状合规。

### 1.4 测试钉住（8 项）

- `PiiRevealReasonEnforcementTests`（5）：空白/全空白理由 → ValueError 且零审计（document/phone）；带理由 → 返回完整值 + AuditEvent.reason 逐字保存 + after 无明文；权限检查先于理由检查。
- `PiiRevealApiReasonTests`（3）：API reason 缺失/全空白 → 400 且零审计；成功揭示时 AuditEvent 必含 reason。

---

## 2. §10.2 敏感导出边界现状（只验证不大改）

| # | 路由 | 现状断言（测试钉住） |
|---|---|---|
| 1 | `/export/staff/` | 旧通道永久关闭：员工 GET → **410**；匿名 → 302 登录墙 |
| 2 | `/export/staff/request/` | `_has_permission("hotelapp.export_pii")` 权限码门禁：无权限（前台）→ **403**；财务 → **201** 建 PENDING ApprovalRecord（幂等键经 `Idempotency-Key` HTTP 头） |
| 3 | `/export/staff/approve/<id>/` | 另一位值班经理审批 → **200** + CSV（含薪酬列）+ `privacy.export_staff` 审计；**申请人自批 → 409**（ApprovalService SoD，Q-05）；非值班经理/店长 → 403 |
| 4 | `/export/<module>/` | PLANNED 模块（food/park/order）→ **302 /home/**（模块注册表检查先于角色检查，店长也不例外）；非管理者（前台）→ **403**（`_has_role` 现状，见 §4 F-06） |

**F-06 现状确认**：`request_staff_export` 已用权限码 `hotelapp.export_pii`（财务/值班经理/店长持有），但 `approve_staff_export`（views.py `_has_role("值班经理","店长")`）与 `export_module`（`_has_role("店长","财务")`）仍为角色名硬编码。本次**仅测试钉住现状 + 登记**，改造列为 Phase 3 权限矩阵迁移项（注册表 `staff` 模块 notes 与 `export_pii` special note 已有同口径登记）。

---

## 3. §10.3 模块门禁覆盖（`RouteGateCoverageTests` + `PlannedModuleGateTests`）

### 3.1 PLANNED 模块行为（保持 302 现状）

- `show_food/ show_park/ show_order/ search_food/ search_park/`：登录员工 GET → **302 /home/**，重定向响应体零业务数据（以预置旧表记录关键字逐一反证：菜名/姓名/房号/车牌均不出现在响应）。
- POST 同样 302 且旧表（Client/Accommodation/Staff/Vip/Room/Order/Food/Park）计数不变。
- `/export/food|park|order/`：302 /home/（§2 #4）。
- UI 行为保持 302 未改（§10.3 的 404/410 建议超出本任务范围，未采纳）。

### 3.2 路由门禁覆盖率检查（白名单式，防新增路由漏门禁）

实现：`django.urls.get_resolver()` 枚举 `hotel/urls.py` 全部 URLPattern（跳过两个豁免 include：`api/v1/`——门禁在 `api_view` 层另有测试；`admin/`——is_staff + 模型权限域），每条路由必须落入以下之一，否则测试失败：

| 分类 | 判定 | 登记 |
|---|---|---|
| 公开面白名单 | view name ∈ PUBLIC_VIEW_NAMES | `login_view`、`logout_view`、`register_view`、`booking_page`、`public_booking_page`、`swagger_ui`、`redoc_ui`、`management_center_page`（视图内自校验 AuthorizationService） |
| 平台域 | `__module__` ∈ apps.identity.platform / permission_admin / permission_center | `/platform/` 7 条（admin_sessionid 会话域 + superuser-only，§6.1） |
| 302 兼容跳转 | `RedirectView` | `/permissions/`、`/permissions/save/` |
| RETIRED-410 | view name ∈ RETIRED_VIEW_NAMES（35） | 10-routes.md §E |
| 有门禁证据 | `_gate_evidence()`：沿 `__wrapped__` 链检测 ① `pms_permission_required` 闭包中的 `app_label.codename` 权限码 → perm；② `module_gate("food"/"order"/"park")` 闭包键 → module_gate；③ `staff_required.wrapped` 同时调用 `_has_active_employment` + `_must_change_password`（co_names 特征）→ staff_gate | 其余全部业务路由 |

实测：86 条项目路由（除 admin/api-v1 include 外）全部可归类，**0 条漏门禁**。另设清单防漂移断言：RETIRED_VIEW_NAMES 中每个视图都必须仍挂在 urlpatterns 上（退役视图被误删时提示更新清单）。

---

## 4. §10.4 旧入口无旁路终验（`LegacyEntryNoBypassTests`）

### 4.1 RETIRED-410 路由全量 POST 终验

- 清单口径：`docs/audit/phase0/10-routes.md` §E1-E35 = **35 条**（含 `/export/staff/` 旧通道；任务书"33 条"为旧口径）。测试以 §E 35 条固定清单钉住。
- **在职员工（全权限店长）POST**：27 条 → **410**（先门禁后 410）；8 条 PLANNED 模块路由（`insert_food/delete_food/update_food/insert_park/delete_park/update_park/insert_order/delete_order`）→ **302 /home/**（`module_gate` 装饰在最内层、先于 410 硬返回执行——fail-closed 顺序的正确现状，测试按此分别钉住）。
- **匿名 POST**：302 登录墙（`staff_required` 的 login_required 先拦，Location 以 `/login/` 开头）或 410，无其他状态。
- **零写入**：全部请求前后 Client/Accommodation/Staff/Vip/Room/Order/Food/Park 八表计数一致（员工与匿名两轮均验）。

### 4.2 /register/ /booking/ /cancel_booking/ 终验

| 入口 | GET | POST | 写入 |
|---|---|---|---|
| `/register/` | 302 `/reserve/`（匿名） | 302 `/reserve/`（匿名） | 不创建 User、旧表计数不变 |
| `/booking/` | 302 `/reserve/` | 302 `/reserve/` | 零写入 |
| `/cancel_booking/` | 匿名 → 302 `/login/`（保留 `staff_required`）；员工 → 302 `/reserve/` | 员工 → 302 `/reserve/` | 零写入 |

注：`/cancel_booking/` 匿名侧为登录墙而非 `/reserve/`——视图保留 `@staff_required`（10-routes.md §B4 同口径："staff_required（残留）；整函数 302→/reserve/"），员工无论 GET/POST 一律 302 `/reserve/` 且不触旧表，无旁路成立。

### 4.3 ORM 直写入口审计（管理命令 grep）

全量扫描 `apps/*/management/commands/` + `hotelapp/management/commands/`（8 个命令）对旧 hotelapp 模型（Client/Accommodation/Staff/Vip/Room/Order/Food/Park）的 create/update/get_or_create/save 调用：

| 命令 | 旧模型接触 | 结论 |
|---|---|---|
| `hotelapp/audit_employee_data` | `Staff.objects.all()` 只读 | 只读盘点（命令自述"只读不改库"），输出 CSV |
| `hotelapp/seed_hotel_data` | 读旧 Staff/Client，仅用于填 `LegacyRecordLink`（core 新模型） | **零旧模型写入**；其余写入全为新模型（inventory/guests/identity/reservations/User）——测试数据种子，允许 |
| `hotelapp/issue_booking_tokens`、`release_expired_reservations` | 无 / 仅 reservations 新模型 | 无旧写入 |
| `hotelapp/seed_role_test_accounts` | 无（User + Group） | 无旧写入 |
| `apps/identity/bind_staff_user`、`platform_superadmin_check` | 无（identity 新模型 / 只读） | 无旧写入 |
| `apps/inventory/sync_inventory_calendar` | 无（inventory 新模型） | 无旧写入 |

**结论：管理命令层对旧模型的 ORM 直写入口 = 0**。旧表唯一写入面是 35 条已 410 的 HTTP 路由（本节已验零可达）与 Django Admin（NoAdminDeleteMixin + is_staff 域，另有治理）。

---

## 5. F-06 / F-08 现状确认（登记，不改造）

| 缺口 | 现状 | 本次动作 |
|---|---|---|
| **F-06**（敏感导出审批侧 `_has_role` 硬编码） | 本任务实施时点：`approve_staff_export` = `_has_role("值班经理","店长")`；`export_module` = `_has_role("店长","财务")`；申请侧已是权限码 `hotelapp.export_pii` | 测试钉住现状（7 项）：403/201/200/409/302 全链行为正确。并行 F-06 迁移任务（`hotelapp.export_approve` 权限码 + registry/views 改造）已在本报告撰写期间开工，本组测试对两种实现均成立（无权限 → 403 / 自批 SoD → 409 不依赖实现方式），迁移完成无需改测试 |
| **F-08**（reverse_checkout / room_release_ready 唯一检查点随 410 退役） | `/uncheckout/`、`/room_status/` 实测 410（本轮 §E 终验覆盖）；PMS2 红字冲销暂无独立权限码（`SPECIAL_PERMISSIONS.reverse_entry` 预留 `hotelapp.reverse_checkout`） | 确认退役面无复活入口（覆盖率测试中两视图仍登记 RETIRED）；无代码变更 |

---

## 6. 测试结果

| 套件 | 数量 | 结果 |
|---|---|---|
| `apps/identity/test_module_gates.py`（本次新增） | 23 | OK |
| ├ PiiReveal 服务层 + API 层 | 8 | OK |
| ├ 敏感导出边界（F-06 现状） | 7 | OK |
| ├ 模块门禁（PLANNED + 覆盖率） | 4 | OK |
| └ 旧入口终验（410 + 302） | 4 | OK |
| 本人领域回归（+ `apps.api.test_security`、`hotelapp` PII/门禁/注册相关类、`apps.guests`） | 34 | OK |
| **全量**（`manage.py test --noinput`，sqlite） | 288 基线 → 334 | 实施完成时点 334 项 **OK**（0 failed）；报告收尾时点 334 项中余 1 项失败：`test_registry.test_module_codes_traceable_to_phase0_audit_docs`——并行 F-06 迁移新增的 `hotelapp.export_approve` 尚未登记进 11/12 号 Phase 0 文档（该交叉核对属并行迁移任务收尾，与本任务变更无关） |
| 基线对照 | 288 → 334 | 无回归（新增 46 项 = 本次 23 + 并行任务 23） |

### 遗留与建议（不阻塞）

1. F-06 审批/导出侧 `_has_role` → 权限码迁移：Phase 3 矩阵迁移时以 `identity.staff_manage` / `hotelapp.export_pii` / 模块 view 码承接。
2. `/cancel_booking/` 匿名侧如需统一 302 `/reserve/`，可摘除残留 `@staff_required`（一行装饰器变更，属 §10.4 收尾项，本次保持现状未动）。
3. PLANNED 模块页面 302 vs 404/410（§10.3 建议）：保持 302 现状；如后续采纳 404/410，仅需改 `module_gate` 单点并更新 `PlannedModuleGateTests`。
