# Phase 0 审计 11 —— 全量权限点与执行位置清单（工作流 C）

- 审计基线：`main` @ `100a93a1382ca310aae588bc3a1734e4d67e94ba`（与 10-routes.md 同基线）。
- 方法：① 全仓 grep（`Permission.objects.get_or_create` / `permissions.add` / `has_permission` / `has_perm` / codename 字符串）；② `DJANGO_DB_ENGINE=sqlite`（`:memory:`）迁移后枚举 `django.contrib.auth.models.Permission/Group` 权威现状（只读，不触碰真实库）；③ 逐执行点读源码核对。
- 运行时授权唯一机制：`AuthorizationService.has_permission(user, property, "app_label.codename")`（apps/identity/authorization.py:35-63）——按 `StaffRoleAssignment→Group→Permission` 且限定 DEFAULT 物业有效任职计算；**superuser 不自动绕过**（authorization.py:22-24）。`User.has_perm()/User.groups` 不参与业务授权（authorization.py:11-13；hotelapp/views.py:1860-1864 注释明确）。
- 本报告不含任何凭据、token、会话键或完整手机号/证件号。

## 1. 权限点总量（迁移后权威枚举）

`Permission` 表共 **263** 行（含 Django 内建）：

| app | 数量 | 构成 |
|---|---|---|
| hotelapp | 53 | 11 模型 × 默认 CRUD 44 + 自定义高危/敏感 9 |
| reservations | 44 | 11 模型 × CRUD（全部默认） |
| core | 37 | 9 模型 × CRUD 36 + `core.breakglass_review` |
| billing | 38 | 9 模型 × CRUD 36 + `refund_request`/`refund_approve` |
| inventory | 33 | 8 模型 × CRUD 32 + `inventory.manage_rates` |
| guests | 21 | 5 模型 × CRUD 20 + `guests.document_reveal`（孤儿，见 §5） |
| identity | 13 | 3 模型 × CRUD 12 + `identity.staff_manage` |
| auth/admin/contenttypes/sessions | 36 | Django 内建 |

组持有现状（枚举输出）：店长 39、值班经理 31、前台 20、财务 13、客房服务员 2、夜审员 0、餐饮服务员 0、餐饮主管 0、停车安保 0（后 4 组为 0014 预留空组，hotelapp/migrations/0014_role_actions.py:43）。

角色组与授权的迁移演进链：identity/0001（英文组 FRONT_DESK 等播种）→ hotelapp/0005（中文 4 大组建组授权）→ hotelapp/0014（9 角色目录 + 7 个高危权限点）→ hotelapp/0015（删英文组）→ hotelapp/0016（add-only 权限补齐 + User.groups 镜像 + 演示账号身份链）→ hotelapp/0017（PII 揭示 2 点）→ hotelapp/0018（财务 view_staff）→ identity/0004（staff_manage + D-04 回收 is_staff）→ billing/0003+0008（退款 2 点）→ core/0007（breakglass_review）→ inventory/0004（manage_rates）→ guests/0003（document_reveal，无授予）。

---

## 2. 自定义/业务权限点明细（15 个）

判定口径：**真实执行** = 存在未被 410/死代码阻断的运行时检查；**仅定义未执行** = 无任何代码检查；**代码检查但无生效执行点** = 检查点存在但全部位于 410 退役函数体内。

| 权限点 | 定义位置 | 授予来源 → 组 | 运行时执行点 | 判定 |
|---|---|---|---|---|
| `hotelapp.finance_access` | hotelapp/migrations/0014_role_actions.py:35-36（挂 financerecord CT） | 0014 ACTION_GRANTS → 财务/值班经理/店长 | hotelapp/views.py:2213（/finance/ 页）；apps/api/guest_center.py:432-444（financial-summary）；views.py:1980（home 财务卡展示） | 真实执行 |
| `hotelapp.export_pii` | 0014:37-38 | 0014 → 财务/值班经理/店长 | hotelapp/views.py:283（/export/staff/request/ 申请侧）；审批+下载侧用 `_has_role` 硬编码（见缺陷 P-C-4） | 真实执行（申请侧） |
| `hotelapp.night_audit_execute` | 0014:29-30 | 0014 → 值班经理/店长 | hotelapp/views.py:2263（旧夜审工作台页，仍 ACTIVE）；apps/api/night_audit.py:42,54（preview/run） | 真实执行 |
| `hotelapp.business_date_reopen` | 0014:31-32（"预留"） | 0014 → 值班经理/店长 | apps/api/night_audit.py:71,88（reopen 申请/批准）、107,132（break-glass 申请/执行） | 真实执行（P2 已落地） |
| `hotelapp.reverse_checkout` | 0014:33-34 | 0014 → 值班经理/店长 | 唯一检查点 hotelapp/views.py:2143 在 `uncheckout`（已 410，views.py:2140 硬返回）；views.py:2028 仅控制按钮展示 | **代码检查但无生效执行点** |
| `hotelapp.room_release_ready` | 0014:25-26 | 0014 → 值班经理/店长 | 唯一检查点 hotelapp/views.py:1433 在 `room_status_change`（已 410，views.py:1419） | **代码检查但无生效执行点** |
| `hotelapp.no_show_handle` | 0014:33-34 | 0014 → 值班经理/店长 | hotelapp/views.py:1361（/no_show_one/ ACTIVE）；apps/api/front_desk.py:968（no-show API）+ 529（能力位展示） | 真实执行 |
| `hotelapp.guest_document_reveal` | hotelapp/migrations/0017:13-14 | 0017:20-21 → 前台/值班经理/店长 | hotelapp/views.py:1508（装饰器）；apps/guests/services.py:78-82（服务层复核）；apps/api/guest_center.py:333-340 | 真实执行（三层） |
| `hotelapp.guest_phone_reveal` | 0017:13-14 | 0017:20-21 → 前台/值班经理/店长 | hotelapp/views.py:1536（装饰器）；apps/api/guest_center.py:366-373 | 真实执行 |
| `identity.staff_manage` | apps/identity/migrations/0004:14-27 | 0004:25-27 → 店长 | hotelapp/views.py:1588-1797（staff_accounts 全部 8 视图装饰器）；apps/api/staff_center.py:81,105；apps/api/reservation_center.py:88（audit-timeline）；apps/api/pages.py:55（staff 中心页） | 真实执行 |
| `billing.refund_request` | apps/billing/models.py:382-385（Meta）；billing/0003_refund.py:12-18 | billing/0003:21-27 → 前台/财务；billing/0008_grant_refund_permissions.py:25-38 同语义补授 | apps/api/front_desk.py:1008（request_refund_api 装饰器）；530,617（能力位） | 真实执行 |
| `billing.refund_approve` | models.py:382-385；billing/0003:16-17 | billing/0003:24-27 → 值班经理/店长；billing/0008:31-38 → 同 | apps/api/front_desk.py:1029（approve_refund_api）；apps/api/approval.py:17-25（_check_approver，审批通用门） | 真实执行 |
| `core.breakglass_review` | apps/core/models.py:212-214（BusinessDate Meta）；core/0007_break_glass_record.py:13-17 | core/0007:16-17 → 值班经理/店长 | apps/billing/night_audit.py:423-425（review_break_glass）；apps/api/night_audit.py:157 | 真实执行 |
| `inventory.manage_rates` | apps/inventory/models.py:163-166（RatePlan Meta）；inventory/0004_rateplan_manage_rates.py:10-15 | inventory/0004:15-16 → 店长 | apps/api/guest_center.py:402-403（adjust_daily_rate_api） | 真实执行 |
| `guests.document_reveal` | apps/guests/migrations/0003_guestdocument_reveal_permission.py:9-17 | **无任何组持有**（迁移注释：按 D-09 待评审分配，0003:4） | 全仓无检查；代码实际使用 `hotelapp.guest_document_reveal`（guests/services.py:76-82 注释声明其为"唯一正式权限码"） | **仅定义未执行 + 无组持有** |

---

## 3. hotelapp 默认 CRUD 权限点（44 个）——执行位置与组持有

检查机制：`@pms_permission_required('<code>')`（identity/decorators.py:21-45）挂旧视图；PMS2 API 函数内 `AuthorizationService.has_permission`。410 路由的检查点一律视为死代码（函数体首行硬返回 410）。

### 3.1 有活跃执行点的 CRUD（14 个）

| 权限点 | 活跃执行点 | 持有组 |
|---|---|---|
| view_staff | views.py:233-234（/index/）、425-426（/search_staff/） | 店长、财务 |
| view_client | views.py:496-497、580-581；api/guest_center.py:99-100、124-125 | 前台、值班经理、财务、店长 |
| add_client | api/guest_center.py:290（guests/create） | 前台、值班经理、店长 |
| change_client | api/guest_center.py:313-314（guests/<id>/update） | 前台、值班经理、店长 |
| view_food | views.py:593-594、683-684（module_gate 关闭，实际不可达但门禁在） | 前台、值班经理、财务、店长 |
| view_vip | views.py:697-698、783-784；api/staff_center.py:126、165（memberships） | 前台、值班经理、财务、店长 |
| add_vip | api/staff_center.py:207-208（memberships/create） | 前台、值班经理、店长 |
| change_vip | api/staff_center.py:246-247（memberships/<id>/level） | 前台、值班经理、店长 |
| view_park | views.py:796-797、884-885（module_gate 关闭，不可达） | 前台、值班经理、财务、店长 |
| view_room | views.py:898-899、991-992；api/guest_center.py:159、203、224（rooms/status、room-types、rate-calendar） | 前台、值班经理、财务、店长、客房服务员 |
| change_room | apps/api/front_desk.py:91-92（_front_desk_write_required，7 条写接口） | 值班经理、店长、客房服务员 |
| view_order | views.py:1004-1005、1087-1088（module_gate 关闭，不可达） | 前台、值班经理、财务、店长 |
| view_accommodation | views.py:1106-1107、1220-1221、2006-2011、2016-2021、1386；api/front_desk.py:69、91；api/reservation_center.py:19、52；api/pages.py:163 | 前台、值班经理、财务、店长 |
| change_accommodation | apps/api/front_desk.py:92（_front_desk_write_required） | 前台、值班经理、店长 |

### 3.2 死代码检查点（原执行路由已 410，共 16 个）

| 权限点 | 死代码位置（410 函数体内） | 持有组 | 判定 |
|---|---|---|---|
| add_staff | views.py:359-360（/insert/） | 店长 | 已授予，无生效执行点 |
| change_staff | views.py:442-443（/update_staff/）、253-254（/staff_toggle/） | 店长 | 已授予，无生效执行点 |
| add_accommodation | views.py:1115-1116、1244-1245 等 | 前台、值班经理、店长 | 已授予，无生效执行点 |
| add_park / change_park | views.py:806-807、855-856 等 | 前台、值班经理、店长 | 已授予，无生效执行点 |
| add_food / change_food | views.py:603-604、655-656 | 店长 | 已授予，无生效执行点 |
| add_order / change_order | views.py:1014-1015 等 | 前台、值班经理、店长 | 已授予，无生效执行点 |
| delete_staff | views.py:410-411 | **无组持有** | 双重失效 |
| delete_client | views.py:538-539 | **无组持有** | 双重失效 |
| delete_food | views.py:639-640 | **无组持有** | 双重失效 |
| delete_vip | views.py:741-742 | **无组持有** | 双重失效 |
| delete_park | views.py:839-840 | **无组持有** | 双重失效 |
| delete_room | views.py:944-945 | **无组持有** | 双重失效 |
| delete_order | views.py:1060-1061 | 值班经理、财务、店长 | 已授予，无生效执行点 |
| delete_accommodation | views.py:1163-1164 | 值班经理、财务、店长 | 已授予，无生效执行点 |

说明：删除类业务在 PMS2 由软流程替代（冲账 `billing.reverse_entry` ApprovalRecord 审批流，apps/billing/services_v2.py:500 区块；Admin 侧 NoAdminDeleteMixin 禁物理删除，hotelapp/admin.py:20+），因此 hotelapp.delete_* 无活跃执行点是设计结果而非漏网；但矩阵页（permission_admin.py:36 ACTIONS 含"删除"列）仍可授予这些无执行点的权限，产生"授予了却什么都不发生"的误导。

---

## 4. PMS2 新应用默认 CRUD（180 个）——仅定义未执行（设计预期）

`billing 36 + core 36 + guests 20 + inventory 32 + reservations 44 + identity 12 = 180` 个默认 CRUD 权限点**无任何组持有、代码亦无 has_perm 检查**。业务写入全部经服务层（services/front_desk_services/lifecycle_services 等）+ 视图层 `AuthorizationService` 检查对应的 hotelapp/identity/billing 专项权限码（见 §2/§3.1），不走 Django 默认 CRUD 码。

- 风险面评估：Django Admin 是这些权限的潜在消费者，但 D-04 已回收全部非 superuser 的 `is_staff`（identity/0004:38-41），业务账号进不了 /admin/；Admin 模型页删除能力被 NoAdminDeleteMixin/只读 Admin 关闭。`reservations.add_bookingaccesstoken` 等 token 类权限同样无 Admin 暴露（apps/reservations/admin.py 仅注册 Reservation/Stay）。
- 结论：**设计预期下的"仅定义未执行"**，但建议 Phase 1 用脚本固化"组持有集合"快照，防止矩阵页/迁移漂移意外授予。

---

## 5. 缺陷清单

### 5.1 "仅定义未执行"缺陷

| 编号 | 缺陷 | 证据 |
|---|---|---|
| P-C-1 | **`guests.document_reveal` 双重孤儿**：guests/0003 定义并声称"揭示证件唯一权限"路线，但全仓无检查点、无组持有；代码实际用 `hotelapp.guest_document_reveal`（services.py:76 注释自认 hotelapp 码为唯一正式码）。两套并存易误导后续开发者 | guests/0003:13-18；guests/services.py:76-82；Permission 枚举 groups=[] |
| P-C-2 | **`hotelapp.reverse_checkout`、`room_release_ready`、`add/change_{staff,food,order,park,room,accommodation}` 等 16 个 CRUD 检查点全部位于 410 死代码**，其中 6 个 delete_* 还无组持有；PMS2 尚未提供反结账/净房放行 API，值班经理/店长持有的 reverse_checkout、room_release_ready 当前"有权无处使" | §3.2 全表；views.py:1419-1433、2140-2143 |
| P-C-3 | **OpenAPI 契约与实现漂移**：`POST /api/v1/daily-rates/{id}/adjust` 文档标注权限 `hotelapp.change_room`，实现检查 `inventory.manage_rates`（文档还漏掉 core/billing 专项码的准确表述）。按文档联动的客户端权限预期会错 | apps/api/openapi_schema.py:175 vs guest_center.py:402-403 |

### 5.2 "无组持有"缺陷

| 编号 | 缺陷 | 证据 |
|---|---|---|
| P-C-4 | **敏感薪酬导出审批绕开权限矩阵**：`approve_staff_export` 用 `_has_role(user,'值班经理','店长')` 硬编码判断，`export_module` 用 `_has_role('店长','财务')`——矩阵页（/permissions/）无法调整这两个动作；且与 API 退款审批（billing.refund_approve 权限码化，approval.py:17-25）标准不一。若矩阵把某动作收回，这两处仍按角色名放行/拒绝 | hotelapp/views.py:320、1466-1470；permission_admin.py:37,40-63 |
| P-C-5 | hotelapp.delete_staff/client/food/vip/park/room 共 6 点**检查点死代码 + 无组持有**双重失效（若未来解除 410 将直接落入无授权兜底，需先补授权评审） | §3.2；views.py:410-411,538-539,639-640,741-742,839-840,944-945 |
| P-C-6 | 180 个 PMS2 新应用默认 CRUD 权限点无组持有（当前设计预期，见 §4），但矩阵页/未来迁移一旦批量授予即产生无物业上下文的越权通道（AuthorizationService 要求任职，Admin 通道则不吃这层） | §4；identity/0004:38-41 |

### 5.3 观察项（非缺陷，供 Phase 1 决策）

1. 双轨授权载体：hotelapp/0016 将岗位组同步镜像到 `User.groups`（0016:31-40 注释），但 AuthorizationService 只读 StaffRoleAssignment（authorization.py:55-63）——镜像目前无消费方（Django Admin 因 is_staff 回收不可达），属冗余面。
2. dev/test 演示账号由 hotelapp/0016 以固定密码创建（0016:55-60、150-172），prod 有门禁与禁用逻辑（0016:167-176）；轮换/删除策略待 Phase 1 确认（本报告不含口令值）。
3. `identity/0001` 曾以英文组（FRONT_DESK 等）播种权限，已被 0015 删除，历史迁移不可回滚——审计新库时应以 §1 枚举结果为准。
4. 矩阵页保存白名单 = MODULES(8 模块×4 动作) + ACTION_PERMS(9 高危点)（permission_admin.py:25-63），**不含** billing/inventory/core/identity 专项码——这些点只能靠迁移授予，矩阵页无法回收（billing.0008、inventory.0004 等的授予在页面之外），SoD 变更需走迁移评审。

---

## 6. 结论摘要

- 权限点总数 263（自定义业务点 15 + hotelapp 默认 CRUD 44 + PMS2 新应用默认 CRUD 180 + Django 内建 24）。
- 真实执行的自定义点 12/15；`guests.document_reveal` 仅定义未执行；`reverse_checkout`、`room_release_ready` 检查点已随 410 退役悬空。
- 旧 CRUD 门禁面：44 个 hotelapp CRUD 点中 14 个有活跃执行点、16 个死代码（6 个叠加无组持有）、14 个仅被不可达页面（module_gate 关闭的 food/order/park 读侧）引用。
- 最需 Phase 1 优先处理：P-C-3（契约漂移）、P-C-4（角色名硬编码绕开矩阵）、P-C-1（孤儿权限点清理）。


### 2026-11-06 追加：夜审查看与执行权限分离

| 权限码 | 归属 | 授权口径 | 执行点 |
|---|---|---|---|
| `core.night_audit_view` | `core.BusinessDate` 自定义权限 | 迁移时复制原 `hotelapp.night_audit_execute` 的现有角色组授权，不扩大原可见人群 | 夜审预览、营业日列表/详情、运行历史、break-glass 只读 API 和中心页面 |
| `hotelapp.night_audit_execute` | 历史高危专项权限 | 保持原值班经理/店长授权 | 夜审正式执行；页面执行按钮；不再用于只读 API |

本次变更只将查看与执行分开，既有可查看角色集合由数据迁移从原执行权限组复制；不会自动给夜审员预留组增加权限。若未来启用夜审员角色，应另行审核授权矩阵。


### 2026-11-06 追加：清洁与维修专项权限拆分

| 权限码 | 授权迁移规则 | 执行边界 |
|---|---|---|
| `inventory.housekeeping_view` | 复制现有 `hotelapp.view_room` 的组授权 | 只读清洁维修工作台，不等于房型/库存主数据权限 |
| `inventory.housekeeping_update` | 复制现有 `hotelapp.change_room` 的组授权 | 清洁状态链；API 与领域服务双重校验 |
| `inventory.maintenance_manage` | 复制现有 `hotelapp.no_show_handle` 的组授权 | 维修停用/停售/恢复；与未到订单处理权限分离，原因必填 |

迁移采用权限组映射复制，保持原有角色可操作集合不扩大；不自动给其他角色增加维修或清洁授权。


## PMS2 财务闭环后续新增专项权限（2026-11-06 修订）

以下权限在 Phase 0 快照之后由 PMS2 财务三方分离与班次现金对账整改引入，纳入统一权限注册表及权限中心，不回写旧历史迁移：

| 权限码 | 迁移 | 授权角色 | 真实执行点 | 语义 |
|---|---|---|---|---|
| `billing.refund_execute` | billing.0013 | 财务 | `apps/api/billing_center.py:execute_refund_api`；`apps/billing/services_v2.py:execute_refund` | 执行已批准退款；执行人必须区别申请人与审批人 |
| `billing.cashier_shift_manage` | billing.0014 | 前台、财务、值班经理、店长 | `apps/api/billing_center.py:open_shift_api/close_shift_api` | 开立与关闭收银班次；前台不因此获得财务明细查看权 |
| `billing.cash_movement_manage` | billing.0013 | 财务 | `apps/api/billing_center.py:shift_cash_movement_api`；`apps/billing/services_v2.py:record_shift_movement` | 登记班次现金补入/支出；按班次幂等、留原因和审计 |

退款权限语义现在为三方分离：`billing.refund_request`（申请）、`billing.refund_approve`（审批）、`billing.refund_execute`（执行）。班次权限与 `hotelapp.finance_access` 分离：前台可以开闭本人班次，但不能因此读取财务中心敏感明细。


### 2026-11-06 PMS2 冲销双人审批补充

以下权限为 Phase 0 快照后新增，按财务与管理岗位职责分离，不回写旧迁移：

| 权限码 | 迁移 | 授权角色 | 真实执行点 | 语义 |
|---|---|---|---|---|
| `billing.reverse_entry` | billing.0011 + billing.0020 | 财务 | `apps/api/approval.py`（申请白名单）与 `apps/api/billing_center.py:reverse_entry_api`；`BillingService.reverse_entry` | 财务发起冲销申请并执行已批准冲销；不得由审批岗位代执行 |
| `billing.reversal_approve` | billing.0020 | 值班经理、店长 | `apps/api/approval.py:approval_approve`；`ApprovalActionRegistry` | 独立审批红字冲销；审批人与执行人必须不同 |

数据库闭环：`LedgerEntry.approval_record` 关联审批事实；`legacy_workflow` 标记迁移前无法证明审批关联的历史冲销。约束 `ck_ledger_reversal_approval_link` 禁止新红字凭证在没有审批外键时入账。历史记录只标记为旧流程，不补造审批人或审批记录。
