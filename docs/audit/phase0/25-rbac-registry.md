# Phase 0 后续治理 25 —— 跨 App RBAC 注册表（方案 §7.2-7.4 交付）

- 交付基线：`main` @ `ad13c28`（在审计基线 `100a93a` + 紧急安全修复 F-01/F-03 之上）
- 交付物：`apps/identity/registry.py`（**代码注册表**，不做数据库模型、无 migration）+
  `hotelapp/views.py` 集成改造 + `apps/identity/test_registry.py`（25 项测试）+ 本文档
- 数据来源：`11-permissions.md`（263 权限点逐点清单）、`12-roles.md`（九角色快照与漂移）、
  `20-findings-register.md`（F-05/F-06/F-08/F-09 缺口）
- 测试结果：`DJANGO_DB_ENGINE=sqlite manage.py test --noinput` —— **234 项全过**
  （交付前基线 209 项 + 本次新增 25 项；连续 3 次全绿）

## 0. 权威性声明（§7.3）

| 事实源 | 状态 |
|---|---|
| `apps/identity/registry.py` | **唯一权威**。模块启停/角色目录/专项权限的登记与校验均以此为准 |
| `hotelapp/views.py MODULE_REGISTRY` | 已改为从注册表派生（旧门禁键 food/order/park → fnb/parking 模块状态）；`module_gate` 行为不变，food/order/park 仍隐藏，有回归测试钉住 |
| `apps/identity/permission_admin.py MODULES/ACTION_PERMS` | 矩阵页 41 点白名单，**Phase 3 权限中心重构时迁移入注册表**；Phase 2 期间不改（避免矩阵 UI 回归），与本注册表冲突时以注册表为准 |
| `AuthorizationService` | 运行时授权机制不变（`StaffRoleAssignment→Group→Permission` + 物业任职）；本注册表只做定义与校验，不介入运行时 |

## 1. MODULE_REGISTRY_V2 —— 模块注册表（11 个模块）

字段口径：`app_label` = 模块主数据的 PMS2 应用域；权限码以 `actions` 的完整
`app_label.codename` 为准（这正是跨 App 注册表的价值：一个模块映射到多个 Django app）。
`actions` 六键：view/add/change/delete/approve/special；`not_applicable` 表示该动作无独立权限码。

| code | 名称 | app_label | 状态 | route 前缀 | 落地页 | 风险 | 物业域 | 权限码数 |
|---|---|---|---|---|---|---|---|---|
| accommodation | 预订与入住 | reservations | ACTIVE | /checkin | /checkin/ | high | 是 | 6 |
| guests | 客人 | guests | ACTIVE | /manage/guests | /manage/guests/ | high | 是 | 6 |
| rooms | 房态与库存 | inventory | ACTIVE | /manage/rooms | /manage/rooms/ | medium | 是 | 6 |
| memberships | 会员 | guests | ACTIVE | /manage/memberships | /manage/memberships/ | medium | 是 | 4 |
| staff | 员工 | identity | ACTIVE | /manage/staff | /manage/staff/ | high | 是 | 6 |
| finance | 财务 | billing | ACTIVE | /finance | /finance/ | high | 是 | 3 |
| night_audit | 夜审 | core | ACTIVE | /nightaudit | /nightaudit/ | high | 是 | 3 |
| fnb | 餐饮 | hotelapp | **PLANNED** | /show_food | （隐藏） | low | 是 | 8 |
| parking | 停车 | hotelapp | **PLANNED** | /show_park | （隐藏） | low | 是 | 4 |
| legacy | 历史只读 | hotelapp | **RETIRED** | /show_ | （退役方向） | low | 是 | 0 |
| platform | 权限管理（平台） | identity | ACTIVE | /platform | /platform/permissions/ | high | **否** | 0 |

### 1.1 逐模块权限面（actions 明细）

| 模块 | view | add | change | delete | approve | special（专项，见 §3） |
|---|---|---|---|---|---|---|
| accommodation | hotelapp.view_accommodation | add_accommodation | change_accommodation | delete_accommodation（矩阵删除列=冲账语义） | — | no_show_handle、reverse_checkout |
| guests | hotelapp.view_client | add_client | change_client | delete_client | — | guest_document_reveal、guest_phone_reveal |
| rooms | hotelapp.view_room | add_room | change_room | delete_room | — | room_release_ready、inventory.manage_rates |
| memberships | hotelapp.view_vip | add_vip | change_vip | delete_vip | — | — |
| staff | hotelapp.view_staff | add_staff | change_staff | delete_staff | — | identity.staff_manage、hotelapp.export_pii |
| finance | hotelapp.finance_access（页读同码） | — | — | — | billing.refund_approve | finance_access、refund_request、refund_approve |
| night_audit | hotelapp.night_audit_execute（页读同码） | — | — | — | hotelapp.business_date_reopen | night_audit_execute、business_date_reopen、core.breakglass_review |
| fnb | hotelapp.view_food | add_food | change_food | delete_food | — | —（extra_codes：view/add/change/delete_order） |
| parking | hotelapp.view_park | add_park | change_park | delete_park | — | — |
| legacy | —（旧只读页复用各在营模块 view_* 码，不双记） | — | — | — | — | — |
| platform | —（矩阵页 superuser-only 三项不变量，无权限码） | — | — | — | — | — |

说明：
- **fnb 模块合并旧 food+order 两个矩阵行**：餐饮订单权限码经 `extra_codes` 参与
  解析校验与 PLANNED 授权检查，module_gate 键 food/order 均映射至 fnb。
- **legacy 模块不登记独立权限码**：15 条旧只读路由（10-routes §F）的门禁复用
  guests/rooms/memberships 等在营模块的 view_* 码；35 条旧写入口已 410（§E）。
  REITRED 方向 = Phase 5 收口，收口前授权面归属在营模块，避免一码双记。
- **platform 模块无权限码**：/platform/permissions/ 与 /admin/ 走 superuser
  三项不变量（is_active+is_superuser+is_staff）+ 平台会话域，不吃物业任职链。

## 2. ROLE_REGISTRY —— 九角色注册表（`12-roles.md` 快照的结构化）

与 `apps/identity/roles.py BUSINESS_ROLE_NAMES` 逐名对齐（有测试断言集合相等）；
数据库九组与迁移声明链零漂移（12-roles §2.1）。

| key | 名称(=组名) | 状态 | editable | builtin | 风险 | 授权数 | 说明 |
|---|---|---|---|---|---|---|---|
| front_desk | 前台 | ACTIVE | 是 | 是 | medium | 20 | 预订/入住/退房/客人档案/退款申请 + 2 项 PII 揭示 |
| duty_manager | 值班经理 | ACTIVE | 是 | 是 | high | 31 | 当日管理者：夜审/净房放行/未到/退款审批/break-glass |
| finance | 财务 | ACTIVE | 是 | 是 | high | 13 | 财务页/敏感导出/退款申请/冲账执行权（delete 列） |
| owner | 店长 | ACTIVE | 是 | 是 | high | 39 | 全模块 + identity.staff_manage + inventory.manage_rates |
| housekeeping | 客房服务员 | ACTIVE | 是 | 是 | low | 2 | 清洁链流转；净房放行不在内 |
| night_auditor | 夜审员 | **PLANNED** | 是 | 是 | high | 0 | 预留空组（0014 决策 5），人员到位后矩阵页授权 |
| fnb_waiter | 餐饮服务员 | **PLANNED** | 是 | 是 | low | 0 | 预留空组，随 fnb 模块启用 |
| fnb_supervisor | 餐饮主管 | **PLANNED** | 是 | 是 | medium | 0 | 预留空组，随 fnb 模块启用 |
| parking_guard | 停车安保 | **PLANNED** | 是 | 是 | low | 0 | 预留空组，随 parking 模块启用 |

注意（12-roles §3.3 语义漂移的登记提醒）：4 个预留空组各有 1 条演示任职
（test_nightaudit/test_fnb/test_fnbsup/test_parking）。角色启用专项处置前，
它们可通过登录门但无任何业务权限（Fail Closed，无越权，仅语义待清理）。

## 3. SPECIAL_PERMISSIONS —— 专项权限注册表（14 点，跨 App 全量）

口径 = 矩阵页 ACTION_PERMS 9 点 + **矩阵外 5 点**（F-09：billing/core/identity/inventory 域，
矩阵页不可见不可收，只能靠迁移授予——这是本注册表存在的核心动因）。

| key | codename | 模块 | 风险 | 要求原因 | 要求确认 | 持有组 | 备注 |
|---|---|---|---|---|---|---|---|
| guest_document_reveal | hotelapp.guest_document_reveal | guests | high | 是 | 是 | 前台/值班经理/店长 | 三层复核+AuditEvent |
| guest_phone_reveal | hotelapp.guest_phone_reveal | guests | high | 是 | 是 | 前台/值班经理/店长 | 同上 |
| refund_request | billing.refund_request | finance | medium | 是 | 否 | 前台/财务 | 与审批权 SoD 分离 |
| refund_approve | billing.refund_approve | finance | high | 是 | 否 | 值班经理/店长 | approval 通用门 _check_approver |
| **reverse_entry** | **hotelapp.reverse_checkout** | accommodation | high | 是 | 是 | 值班经理/店长 | 见 §6 F-08 处置 |
| room_release_ready | hotelapp.room_release_ready | rooms | medium | 否 | 否 | 值班经理/店长 | 见 §6 F-08 处置 |
| no_show_handle | hotelapp.no_show_handle | accommodation | medium | 否 | 是 | 值班经理/店长 | 页面+API 双执行点 |
| finance_access | hotelapp.finance_access | finance | medium | 否 | 否 | 财务/值班经理/店长 | 页面+API+首页卡执行点 |
| night_audit_execute | hotelapp.night_audit_execute | night_audit | high | 否 | 是 | 值班经理/店长 | 例行日结，不要求原因 |
| business_date_reopen | hotelapp.business_date_reopen | night_audit | high | 是 | 是 | 值班经理/店长 | break-glass 附二次密码验证 |
| breakglass_review | core.breakglass_review | night_audit | high | 是 | 否 | 值班经理/店长 | 矩阵外点（F-09） |
| export_pii | hotelapp.export_pii | staff | high | 是 | 否 | 财务/值班经理/店长 | 申请侧执行；审批侧 F-06 |
| manage_rates | inventory.manage_rates | rooms | medium | 否 | 否 | 店长 | 矩阵外点（F-09）；F-05 契约漂移 |
| staff_manage | identity.staff_manage | staff | high | 否 | 否 | 店长 | 矩阵外点（F-09）；工作台 8 视图+3 API |

`reverse_entry` 的命名说明：红字冲销在 PMS2 由审批流承担
（`LedgerEntryService.reverse_entry`，审计 action_code=`billing.reverse_entry`，
无独立 Django 权限码）；本注册表把该专项语义落点到现实存在的唯一权限码
`hotelapp.reverse_checkout`（0014 定义：反结账=冲销对应收款流水），待 Phase 4
补 front-desk 反结账 API 时以此授权，避免登记一个数据库不存在的“幽灵码”。

## 4. 旧→新映射表：矩阵 41 点 → 注册表全量跨 App 权限

矩阵页白名单 `ALL_CODENAMES`（permission_admin.py）= 8 模块 × 4 动作（32 点）+ 9 高危点 = **41 点，全部 hotelapp 域**。注册表逐点承接如下（✉=登记位置不变；行动作列号对应矩阵列）：

### 4.1 模块 CRUD 32 点

| 矩阵行 | view | add | change | delete | 注册表归属 | 状态备注 |
|---|---|---|---|---|---|---|
| 员工管理(staff) | ✉ | ✉ | ✉ | ✉ | staff 模块 actions | delete 无执行点（11-perm §3.2） |
| 顾客管理(client) | ✉ | ✉ | ✉ | ✉ | guests 模块 actions | delete 无组持有+死代码 |
| 客房管理(room) | ✉ | ✉ | ✉ | ✉ | rooms 模块 actions | view/change 有活跃执行点 |
| 会员信息(vip) | ✉ | ✉ | ✉ | ✉ | memberships 模块 actions | view/add/change 活跃 |
| 客房订单(accommodation) | ✉ | ✉ | ✉ | ✉ | accommodation 模块 actions | delete 列=冲账语义 |
| 餐厅管理(food) | ✉ | ✉ | ✉ | ✉ | fnb 模块 actions | PLANNED；门禁隐藏 |
| 餐厅订单(order) | ✉ | ✉ | ✉ | ✉ | fnb 模块 extra_codes | PLANNED；新增 delete_order 归属 |
| 停车场(park) | ✉ | ✉ | ✉ | ✉ | parking 模块 actions | PLANNED；门禁隐藏 |

### 4.2 高危动作 9 点

| 矩阵 ACTION_PERMS | 注册表 key | 归属模块 |
|---|---|---|
| room_release_ready | room_release_ready | rooms |
| night_audit_execute | night_audit_execute | night_audit |
| business_date_reopen | business_date_reopen | night_audit |
| reverse_checkout | **reverse_entry** | accommodation |
| no_show_handle | no_show_handle | accommodation |
| finance_access | finance_access | finance |
| export_pii | export_pii | staff |
| guest_document_reveal | guest_document_reveal | guests |
| guest_phone_reveal | guest_phone_reveal | guests |

### 4.3 注册表在 41 点之外新增登记（矩阵外，F-09 收敛）

| 权限码 | 注册表 key | 归属模块 | 持有组 | 矩阵页可见性 |
|---|---|---|---|---|
| billing.refund_request | refund_request | finance | 前台、财务 | 不可见、保存时保留 |
| billing.refund_approve | refund_approve | finance | 值班经理、店长 | 不可见、保存时保留 |
| core.breakglass_review | breakglass_review | night_audit | 值班经理、店长 | 不可见、保存时保留 |
| identity.staff_manage | staff_manage | staff | 店长 | 不可见、保存时保留 |
| inventory.manage_rates | manage_rates | rooms | 店长 | 不可见、保存时保留 |

### 4.4 未映射权限点盘点（263 − 46 = 217，INFO 级，validate_registry 自动报告）

| 构成 | 数量 | 处置政策 |
|---|---|---|
| Django 内建（auth/admin/contenttypes/sessions） | 24 | 平台技术域，不属于业务注册表 |
| PMS2 新应用默认 CRUD（billing 36 + core 36 + guests 20 + inventory 32 + reservations 44 + identity 12） | 180 | 仅定义未执行（11-perm §4 设计预期）：业务写入走服务层+专项码；Admin 是潜在消费者但 is_staff 已回收（D-04）。**禁止矩阵页/迁移批量授予**——授权须经本注册表评审后进专项码 |
| hotelapp 未入模模型 CRUD（FinanceRecord/NightAudit/Folio × 4） | 12 | 账务/夜审经服务层写入，无独立执行点；随 Phase 3 矩阵迁移决定去留 |
| 孤儿点 guests.document_reveal | 1 | F-07/P-C-1：仅定义未执行、无组持有；代码实际用 hotelapp.guest_document_reveal。Phase 2 清理（删除或映射，需单独变更） |

## 5. validate_registry() —— 一致性校验（§7.4）

返回发现清单（ERROR/WARNING/INFO，不抛异常）；ERROR 清单为空 = 注册表可发布。

| 级别 | 机读码 | 规则 |
|---|---|---|
| ERROR | duplicate_module_code / duplicate_role_key / duplicate_role_group_name / duplicate_special_key / duplicate_special_codename | 重复 code |
| ERROR | duplicate_permission_reference | 同一权限码被**多个模块**重复登记（同模块内 approve 列与 special 元组同码属预期） |
| ERROR | unknown_permission_code | ACTIVE 模块引用的权限码不在 Permission 表（“未知 codename”规则） |
| ERROR | unknown_special_permission | 专项权限码不在 Permission 表（全量，无状态豁免） |
| ERROR | special_unknown_module / special_not_in_module_tuple / special_tuple_unregistered / special_tuple_module_mismatch / special_bad_risk / special_unknown_field | 专项权限 ↔ 模块 special 元组双向一致 |
| ERROR | missing_role_group | ROLE_REGISTRY 九角色在数据库缺组 |
| WARNING | planned_module_granted | PLANNED 模块权限码仍被组持有（当前存量=历史授予基线，Phase 3 回收） |
| WARNING | planned_role_has_permissions / unregistered_group | 预留角色意外持权 / 数据库出现目录外组 |
| WARNING | planned_code_not_yet_defined | PLANNED/RETIRED 模块引用尚不存在的预留码 |
| INFO | unmapped_permissions | 217 个未映射点清单（§4.4 政策） |

测试钉住（`apps/identity/test_registry.py`，25 项）：
- 一致性 7 项：validate 无 ERROR；必含 11 模块；ACTIVE 码逐点解析 Permission；
  **与 11/12 号审计文档逐码交叉核对**；special 双向一致；未映射盘点必须暴露
  `guests.document_reveal` 与 Django 内建码；PLANNED 存量授权只进 WARNING 基线。
- 伪造 POST 3 项：对 /platform/permissions/save/ 伪造 `perm=add_food&perm=delete_park`
  （PLANNED 模块码）→ **矩阵白名单当前放行**（见 §6 新发现 N-1）→ 断言注册表
  planned_module_granted 必须逐码发现；白名单外码（identity.staff_manage /
  billing.refund_approve）仍被拒绝；PLANNED 基线逐码可归属。
- module_gate 回归 5 项：`hotelapp.views.MODULE_REGISTRY == {food:False, order:False,
  park:False}`；在职员工访问 /show_food/、/show_park/、/export/food/ 均 302 回 /home/；
  对照组 /show_room/ 200。
- 角色 5 项：九组一一对应；5 ACTIVE 组持权、4 PLANNED 组为空；字段形态。
- 专项 5 项：14 点逐点解析；任务书要求的 9 key 在册；字段形态；reverse_entry→
  reverse_checkout 的 F-08 落点；F-09 矩阵外 5 点全在册。

## 6. 漂移说明与缺口处置建议（F-05/F-06/F-08 + 本次新发现）

### 本次新发现（注册表实施过程暴露）

| 编号 | 发现 | 证据 | 处置建议 |
|---|---|---|---|
| N-1 | **矩阵保存白名单未排除 PLANNED 模块权限码**：UI 对餐饮/停车置灰，但服务端 `ALL_CODENAMES` 仍含其 32 点中的 food/order/park 12 点——伪造 POST 可绕过 UI 直接授权（如给客房服务员加 `add_food`）。测试 `PlannedModuleGrantGuardTests` 已钉住：当前放行、注册表逐码发现 | permission_admin.py:62-63 白名单构造 vs 25-34 reserved=True；test_registry.py 伪造 POST 断言 | Phase 3 矩阵迁移时白名单改为"注册表 ACTIVE 模块码 ∪ 专项码"；短期兜底=本注册表校验（测试已固化） |
| N-2 | `hotelapp.add_room` 在 11-permissions.md 无出处（仅 12-roles.md 店长快照含它）——逐点清单对“已授予但无执行点”的 add 侧 CRUD 覆盖不全 | 11-permissions §3.1/§3.2 表 vs 12-roles §1 店长 39 项 | Phase 1 权限快照脚本（方案 §12）以注册表为核对基准补齐逐点出处 |

### F-05 OpenAPI 契约漂移（inventory.manage_rates vs 文档误标 change_room）

- 现状：`POST /api/v1/daily-rates/{id}/adjust` 实际检查 `inventory.manage_rates`（guest_center.py:402-403），OpenAPI 文档标注 `hotelapp.change_room`（openapi_schema.py:175）。
- 注册表处置：`inventory.manage_rates` 已登记为 rooms 模块专项（key=manage_rates，备注注明 F-05）——**权威归属只有一处**。
- 建议：Phase 4 契约核验时以注册表为准修文档；文档生成器后续可消费 `special_codenames_by_module("rooms")` 作为该接口的合法权限表述来源。

### F-06 敏感导出审批硬编码角色名（绕开矩阵）

- 现状：申请侧查 `hotelapp.export_pii`（权限码化），审批+下载侧用 `_has_role(user,'值班经理','店长')`/`_has_role('店长','财务')` 硬编码（hotelapp/views.py:322、1469）——矩阵收回动作时这两处仍按角色名放行，与退款审批（billing.refund_approve 码化）标准不一。
- 注册表处置：`hotelapp.export_pii` 登记（staff 模块，requires_reason=是），note 明确标注 F-06；导出路由在注册表的 route_prefix=/export 归属 staff 模块。
- 建议：Phase 2/3 权限执行点迁移时把审批/下载侧改为 `AuthorizationService.has_permission(user, prop, "hotelapp.export_pii")`（或新增独立 export_approve 专项码并登记入本注册表）；迁移前矩阵页不得回收 export_pii（否则申请/审批两侧门禁错位）。

### F-08 reverse_checkout / room_release_ready 权限悬空（2026-11-06 更新）

- 旧 `hotelapp.reverse_checkout` 的唯一检查点随旧路由 410 退役，旧码不再作为 PMS2 冲销执行授权。
- PMS2 红字冲销现有独立执行点：`billing.reverse_entry`（财务申请/执行）与 `billing.reversal_approve`（值班经理/店长审批）；`ApprovalActionRegistry` 注册 `billing.reversal`，财务执行必须关联经理已批准的 ApprovalRecord。
- `LedgerEntry.approval_record` 与 `ck_ledger_reversal_approval_link` 将审批事实落到数据库；历史旧冲销只标记 `legacy_workflow`，不伪造审批记录。
- `hotelapp.room_release_ready` 仍按 §4.3 的真实执行点核验，不与冲销权限混用。

### F-07 / F-09 状态备注

- F-07（guests.document_reveal 孤儿）：注册表**故意不登记**该码（guests 模块 notes 声明），未映射盘点将其作为孤儿暴露（有测试断言）——保证 Phase 2 清理（删除或映射）不被遗忘。
- F-09（矩阵外权限双轨管理）：**注册表即本缺口的处置本体**——矩阵外 5 点全部入册（§4.3），矩阵页 41 点逐点承接（§4.1/4.2），Phase 3 迁移后 permission_admin.MODULES/ACTION_PERMS 退役为纯 UI 渲染配置。

## 7. 交付清单与测试数字

| 文件 | 变更 |
|---|---|
| `apps/identity/registry.py` | 新建：11 模块 + 9 角色 + 22 专项权限 + 8 个查询/校验函数 |
| `hotelapp/views.py` | MODULE_REGISTRY 改为注册表派生（module_gate/export_module 行为不变） |
| `apps/identity/test_registry.py` | 新建：25 项测试（5 个测试类） |
| `docs/audit/phase0/25-rbac-registry.md` | 本文档 |

- 测试：`DJANGO_DB_ENGINE=sqlite manage.py test --noinput` = **234 项全过**
  （交付前基线 209 项全过 ×3 次确认；新增 25 项；连续 3 次全绿）。
- 禁止项核对：未新增 migration；未改 AuthorizationService 运行时逻辑；
  未改矩阵 UI 与 permission_admin.MODULES/ACTION_PERMS（Phase 3 迁移）。
- 环境备注（非代码问题）：本次实施期间工作区有并行的 §6.1 平台身份域改动合入
  （/platform/ 迁移、模板调整），合入过程中运行的测试出现个别瞬时失败
  （模板/文件枚举竞态）；工作区稳定后连续 3 次全量 234 项全绿确认。


### 2026-11-06 冲销权限矩阵补充

| 权限码 | 授权角色 | 执行边界 |
|---|---|---|
| `billing.reverse_entry` | 财务 | 发起冲销审批申请；仅在审批通过后执行红字凭证 |
| `billing.reversal_approve` | 值班经理、店长 | 审批冲销申请；无财务冲销执行权 |

