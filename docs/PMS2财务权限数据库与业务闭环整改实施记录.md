# PMS2 财务、权限、数据库与业务闭环整改实施记录

> 版本：2026-11-06（实施补充）
> 代码基线：GitHub `wulei0214/django-hotel2` 的 `main`
> 状态：代码整改持续集成中；本文件不替代 MySQL 生产部署与真实页面验收。

## 一、整改结论

本轮不增加 PMS3.0 新业务模块，集中修复 PMS2 上线前的财务正确性、权限语义、数据库完整性和业务闭环问题。

已落实的核心实现：

1. 退款申请、审批、执行三方分离；只有执行成功才产生退款账本凭证。
2. 现金班次按现金事实计算，不再把开班备用金当作应有现金。
3. 现金支付、现金退款、现金补入/支出均关联对应班次；关班与现金变动通过班次行锁串行化。
4. 维修管理权限从未到处理权限中拆出，并按正式角色重新授权。
5. 通知已读采用员工档案 × 通知的个人回执。
6. 财务旧入口退役，财务页面与 API 统一到 PMS2。
7. 账本、审计、班次现金等只追加事实增加 ORM 防旁路与 MySQL 触发器保护。
8. MySQL CI 在全量迁移和测试后安装并检查六条只追加触发器。

**尚未达到最终冻结条件：** 最新 main 的完整 MySQL/SQLite CI 必须全绿；本地目标 MySQL 数据库仍需修复连接凭据并执行真实迁移；生产 MySQL 触发器及运行账号权限需要 DBA 验收；还需完成真实浏览器页面 E2E 与备份恢复演练。

## 二、财务闭环

### 2.1 退款三方分离

退款流程固定为：

| 环节 | 角色责任 | 必须保存的事实 |
|---|---|---|
| 申请 | 前台/财务授权人员 | `requested_by`、金额、原因、原支付、申请幂等键 |
| 审批 | 值班经理/店长授权人员 | `approval_record`、`approved_by`、审批时间 |
| 执行 | 财务授权人员 | `executed_by`、执行时间、渠道参考号、执行幂等键 |
| 入账 | BillingService | 唯一 REFUND LedgerEntry、支付状态、审计事件 |

强制规则：

- 申请人、审批人、执行人必须是三个不同账号。
- 审批必须关联 ApprovalRecord；缺少审批记录不得审批、驳回或执行。
- 申请与审批阶段不产生退款账本凭证。
- 只有确认实际退款完成后，执行阶段才追加 REFUND 凭证。
- 退款金额受原支付可退余额约束；并发退款通过原支付行锁与累计预占金额校验。
- 同一执行幂等键重放返回原退款，不得重复入账。
- 非现金渠道退款必须填写渠道参考号。
- 现金退款必须由执行人本人开立班次，且班次应有现金足以覆盖本次退款。
- 历史上无法证明三方分离的记录通过 `legacy_workflow` 标记保留，只读，不补写虚假的审批/执行事实。

相关实现：

- `apps/billing/services_v2.py`：`request_refund`、`approve_refund`、`reject_refund`、`execute_refund`
- `apps/api/billing_center.py`：退款申请、审批、驳回、执行 API
- `apps/billing/models.py`：Refund 状态机、不可变字段、SoD 校验
- `apps/billing/migrations/0016_refund_sod_state_constraints.py`：历史标记与三方分离数据库约束
- `apps/billing/migrations/0019_refund_requires_approval_record.py`：非待审批退款必须关联审批记录；无法追溯的历史记录只读标记

### 2.2 班次现金口径

班次应有现金统一按以下公式计算：

\[
\text{应有现金}
= \text{开班备用金}
+ \text{本班现金收入}
- \text{本班现金退款}
+ \text{现金补入}
- \text{现金支出}
\]

数据归属：

- 现金收款：`Payment.cashier_shift`
- 现金退款：`Refund.execution_shift`
- 现金补入/支出：`CashierShiftMovement`

规则：

- 非现金支付不进入现金抽屉金额。
- 现金退款归属实际执行退款的班次，不回写原收款班次。
- 现金支出不得超过当前班次应有现金。
- 现金退款不得导致执行班次现金余额透支。
- 关班必须锁定班次行，计算应有现金、录入实盘现金并记录差异。
- 实盘现金和应有现金不得为负；差异非零必须填写原因。
- 已关班禁止新增现金变动；同一现金变动幂等重放即使在关班后也返回原事实。
- 班次现金事实不可修改、删除或批量覆盖。

相关实现：

- `BillingService.cash_shift_expected_cash`
- `BillingService.record_shift_movement`
- `BillingService.close_shift`
- `CashierShift.save`、`CashierShiftMovement` 模型与只追加 QuerySet

### 2.3 收款、押金和退款幂等

- 收款参考号先查重，再检查账单是否仍开放；已成功支付的同参考号重放，即使账单已经结账，也返回原支付。
- 押金幂等键先查重，再检查预订状态和主账单；退房/结账后的重放不会重复增加押金或重复记账。
- 现金变动先按班次和幂等键查重，再校验新支出余额；重放不会因为首次支出已扣减余额而被错误拒绝。
- 幂等键与请求事实不一致时拒绝重放，不能将同一键用于不同金额、方向或原因。

## 三、权限语义与角色映射

### 3.1 正式授权矩阵

| 权限 | 允许角色 |
|---|---|
| `billing.refund_request` | 前台、财务 |
| `billing.refund_approve` | 值班经理、店长 |
| `billing.refund_execute` | 财务 |
| `billing.cashier_shift_manage` | 前台、财务、值班经理、店长 |
| `billing.cash_movement_manage` | 财务 |
| `inventory.maintenance_manage` | 值班经理、店长 |

退款的审批权限与执行权限分离；财务可以申请和执行，但不能凭财务岗位自行审批自己的退款。

### 3.2 维修权限纠正迁移

- `inventory.0005_housekeeping_permission_codes` 曾把 `hotelapp.no_show_handle` 作为维修权限来源。
- `inventory.0006_align_maintenance_manage_role_grants` 清除错误复制的组授权。
- 维修管理只重新授予值班经理、店长。
- 未到处理仍由 `hotelapp.no_show_handle` 控制。
- 清洁流转仍由 `inventory.housekeeping_update` 控制。
- 页面、API、领域服务均使用独立权限语义，不通过改名掩盖历史授权污染。

### 3.3 回归保护

`BillingPermissionMigrationMatrixTests` 对退款、班次、现金变动和维修权限执行正式角色集合断言，防止后续迁移或人工调整造成权限漂移。

## 四、数据库完整性与 MySQL

### 4.1 新增/修订迁移

| 迁移 | 内容 |
|---|---|
| `inventory.0006` | 纠正维修权限错误继承 |
| `core.0013` | 员工个人通知已读回执 |
| `billing.0012` | 退款执行事实、班次现金变动、支付/退款班次关联 |
| `billing.0013` | 退款执行与现金变动权限 |
| `billing.0014` | 班次管理权限 |
| `billing.0015` | 班次现金变动主键修订 |
| `billing.0016` | 退款申请/审批/执行 SoD 与状态事实约束，历史记录标记 |
| `billing.0017` | 现金变动方向只能是 IN/OUT |
| `billing.0018` | 开班备用金、应有现金、实盘现金非负约束；迁移前发现负数历史记录会明确失败，不静默改账 |
| `billing.0019` | 非待审批退款必须关联审批记录；无法追溯的历史记录标记只读 |

### 4.2 ORM 与数据库事实保护

以下事实表禁止 ORM 批量 update/delete/bulk_create/bulk_update，并通过模型保存规则保护状态迁移：

- Payment
- Refund
- ApprovalRecord
- CashierShift
- CashierShiftMovement
- LedgerEntry
- AuditEvent

其中 AuditEvent、LedgerEntry、CashierShiftMovement 还提供 MySQL 8 BEFORE UPDATE/DELETE 触发器，防止具有 ORM 以外 SQL 通道的误改。

MySQL 部署文件：

- `apps/core/sql/audit_immutability.sql`
- `python manage.py install_mysql_append_only_guards`
- `python manage.py check_mysql_append_only_guards`

检查命令会核对六条触发器的名称、表、事件、时机及 `SIGNAL SQLSTATE '45000'` 阻断语句。

### 4.3 MySQL 验收边界

MySQL 8 CI 使用独立服务数据库，执行顺序为：

1. 全量 `migrate`。
2. 完整 Django 测试套件。
3. 安装六条 append-only 触发器。
4. 执行触发器定义检查。

SQLite CI 仍负责 Django system check、migration drift 和完整测试，但不替代 MySQL。

本地曾出现 `Access denied for user 'wu'lei'@'localhost'`。该问题属于本地 MySQL 用户/凭据或环境变量配置，远程代码提交无法替用户创建本地数据库账号。生产或本地最终验收仍需使用目标 MySQL 凭据运行迁移和触发器检查。

## 五、通知个人已读

- `NotificationRead` 以 `notification × staff_profile` 唯一约束保存个人已读回执。
- API 通过当前员工有效任职和角色组计算可见范围。
- 岗位广播中员工 A 标记已读，不会影响员工 B。
- 调岗/离职后旧岗位广播不再可见。
- 旧 `Notification.is_read/read_at` 仅为历史迁移兼容字段，不再作为 API 或侧栏个人已读权威。
- `read_all` 只为当前员工当前物业可见通知创建个人回执。

## 六、页面与 API 统一

- 唯一财务页面：`/manage/finance/`
- 旧页面：`/finance/` → 302 到 `/manage/finance/`
- 旧写入口：`/finance_add/` → 410 Gone，并指向新工作台
- 财务 API 统一：`/api/v1/billing/`
- 退款申请、审批、驳回、执行使用不同动作和权限，不合并为一个“退款”按钮。
- 财务工作台 CSRF 使用 Session token，所有写请求携带 `X-CSRFToken`。
- 财务动态表单在 DOM 替换前读取金额、方式、参考号和原因，防止表单被替换后字段丢失。

## 七、本轮关键回归测试

- `BillingCsrfEnforcementTests.test_billing_write_api_rejects_missing_and_invalid_csrf`
- `BillingRefundThreePartyApiTests.test_refund_api_requires_three_distinct_people_and_posts_once`
- `FinanceLegacyRouteRetirementTests`
- `BillingPermissionMigrationMatrixTests`
- `CashShiftReconciliationTests.test_cash_shift_expected_balance_includes_income_refunds_and_movements`
- `CashShiftReconciliationTests.test_cash_refund_cannot_overdraw_execution_shift`
- `CashShiftReconciliationTests.test_non_cash_payment_is_not_counted_in_cash_shift`
- `CashShiftReconciliationTests.test_closed_shift_rejects_new_cash_movement`
- `ImmutableFinancialFactManagerTests`
- `CollectPaymentClosedFolioReplayTests`
- `DepositGuardTests.test_deposit_replay_survives_checkout_and_folio_close`
- 既有 `CashierShiftClosureTests` 与 `ReconciliationConsistencyTests`
- 既有 NotificationRead 个人回执隔离、幂等和物业隔离测试
- 既有中心模板 CSRF 与财务 DOM 回归测试

## 八、最终冻结门槛

本实施记录不把“代码已提交”视为“上线验收完成”。冻结基线 2.0 前，必须留存：

- [ ] 最新 main 的 Django CI 全绿。
- [ ] 最新 main 的 MySQL 8 全量迁移、测试、六条触发器安装与检查全绿。
- [ ] 目标 MySQL 数据库凭据可用，迁移前备份完成，迁移后 `showmigrations` 无未应用项。
- [ ] 生产 MySQL 触发器存在，运行账号无三张只追加事实表的 UPDATE/DELETE 权限。
- [ ] 备份恢复演练通过。
- [ ] 真实浏览器完成财务收款、退款申请、审批、执行、现金交班、差异说明、审计查询。
- [ ] 真实浏览器完成九角色越权验证。
- [ ] OpenAPI 快照与运行时 schema 一致。
- [ ] 本文件及基线 2.0 的本轮修订记录与最终 main SHA 一致。

以上门槛全部满足后，才能冻结 PMS2 质量基线并启动 PMS3.0 排期。
