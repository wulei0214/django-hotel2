# 55 · 财务、权限、数据库与业务闭环四域验收核查报告

- 核查对象：并行实施流提交的两份整改文档
  - `docs/PMS2财务权限数据库与业务闭环整改实施记录.md`（下称"实施记录"，含 §9/§10 追加整改）
  - `docs/PMS2补充问题复核与整改进度报告_2026-11-06.md`（下称"补充报告"，含 §8 后续整改记录）
- 核查方式：只读核查 + 全量测试；`git pull` 后工作树 clean，main 与 origin/main 一致
- 核查基线：main HEAD `00791aa`（2026-11-06）；本地全量测试 853 项全部通过（244.898s，OK），无回归
- 结论速览：**财务域闭环、权限域闭环；数据库域代码/CI 闭环但存在 1 个部署脚本缺陷；业务闭环域闭环（覆盖面有 1 项备注）**。共登记 4 项缺口 + 1 项覆盖面备注，详见 §6。

## 1. 整改项验证总表（两份文档声明 → 代码/测试/CI 三方核对）

状态含义：✅=代码改动属实+测试存在+声明与实现一致；⚠️=声明与实现基本一致但有缺口（进 §6 登记表）。

| # | 声明来源 | 整改项 | 关键提交 | 代码证据 | 测试证据 | 状态 |
|---|---|---|---|---|---|---|
| 1 | 实施记录 §2.1 | 退款三方分离（申请/审批/驳回/执行/入账） | — | `apps/billing/services_v2.py` request_refund/approve_refund/reject_refund/execute_refund；`apps/billing/models.py` SoD；迁移 0016/0019 | `BillingRefundThreePartyApiTests`、白盒 `test_refund_three_party_flow_posts_once_and_is_idempotent`、e2e `test_refund_request_pends_for_approval_then_owner_completes` | ✅ |
| 2 | 实施记录 §10.2 / 任务项 | 退款审批关联校验 `_validate_refund_approval_link`（approve/reject/execute/到期四路径） | `600d0b9` | services_v2.py:203 四处调用（604/699/766/870） | `test_refund_approval_link_must_match_on_approve_reject_execute`（含 expire_due_refunds 错链拒绝、零 REFUND 凭证断言） | ✅ |
| 3 | 实施记录 §10.2 | 审批参数快照畸形拒绝 | `506b804` | services_v2.py payload isinstance dict 校验 | 同上测试覆盖非 dict/错金额/错 payment_id 路径 | ✅ |
| 4 | 实施记录 §10.4 | 通用审批入口兼容（审批原因不再强制等于退款原因） | `fe89cf0` | `_validate_refund_approval_link` 删除 reason 相等比较，保留物业/动作/实体/申请人/快照校验 | 既有 `test_generic_refund_approval_can_be_consumed_by_billing_service` 通过；错链其他动作仍 fail closed | ✅ |
| 5 | 任务项 | 现金退款执行班次归属校验 | `4bbe16e` | `apps/billing/models.py:619-634`（CASH 退款 SUCCEEDED 必须关联执行人本人本物业班次；非现金不得伪造 execution_shift） | 仅正向路径被 e2e 间接覆盖；**无专属负向测试** | ⚠️ G-02 |
| 6 | 任务项 | 退款审批到期定时命令 | `1bc9aa6`/`8950d8e` | `BillingService.expire_due_refunds`（批量、逐笔事务、锁序 Payment→Refund→ApprovalRecord）+ `apps/billing/management/commands/expire_pending_refunds.py` | `test_scheduled_expiry_command_closes_overdue_refunds`、`test_expired_refund_approval_closes_refund_and_releases_payment_hold`（释放 Payment 预占、零账本副作用） | ✅ |
| 7 | 任务项 | 通用审批入口授权前置（先查权限再提示走退款专用入口） | `d5485c4` | `apps/api/approval.py` approval_approve/reject 先 `has_permission` 再 BUSINESS_CONFLICT 路由守卫 | `test_approve_requires_registry_approve_permission`（财务无 refund_approve → 403 且状态不变）、`test_approve_permission_follows_action_registry` | ✅ |
| 8 | 实施记录 §2.2/补充报告 §8.1 | 班次现金口径 `opening+income-refunds+in-out`；退款归属执行班次 | — | `BillingService.cash_shift_breakdown`（写路径 select_for_update 当前读）；`close_shift` 使用 breakdown | `CashShiftReconciliationTests`×5、closure `test_expected_cash_uses_cash_payments_refunds_and_cash_movements`、e2e 双班次分别关账断言 | ✅ |
| 9 | 实施记录 §9.1 | 开班备用金重放校验/关班审计改用事实明细/null 原因归一化 | — | `open_shift` 备用金不一致拒绝重放；`close_shift` 用 `cash_shift_breakdown`、`variance_reason` strip | `test_open_shift_idempotency_rejects_different_opening_float`、`test_close_shift_accepts_null_reason_when_cash_balances` | ✅ |
| 10 | 实施记录 §2.3 | 收款/押金/现金变动幂等（关账后重放返回原事实） | — | collect_payment/record_deposit/record_shift_movement 先查重后校验 | `CollectPaymentClosedFolioReplayTests`、`DepositGuardTests::test_deposit_replay_survives_checkout_and_folio_close`、`test_cash_movement_duplicate_race_returns_existing_fact` | ✅ |
| 11 | 实施记录 §9.3 | 红字冲销经理审批闭环（billing.reversal + 0020 + 数据库约束） | — | 注册表 `billing.reversal`（request=reverse_entry/approve=reversal_approve）；`reverse_entry` 校验申请/审批专项权限、审批人≠执行人、原因完全一致；迁移 0020 回收管理岗执行权、`ck_ledger_reversal_approval_link` 约束、legacy 标记 | e2e `test_reverse_entry_posts_paired_reversal_and_audit`（财务申请→店长审批→财务执行）、白盒 `test_new_reversal_requires_approval_record_but_legacy_marker_is_allowed`、矩阵测试 | ✅ |
| 12 | 实施记录 §3.1 | 正式授权矩阵（6 权限 × 角色） | — | `apps/identity/registry.py` 439-471 权限登记与备注 | `BillingPermissionMigrationMatrixTests` 逐一断言角色集合（refund_request=前台/财务、refund_approve=值班经理/店长、refund_execute=财务、cashier_shift_manage=四角色、cash_movement_manage=财务、maintenance_manage=值班经理/店长） | ✅ |
| 13 | 实施记录 §3.2 | 维修权限纠正（与 no_show_handle 解耦） | — | 迁移 `inventory.0005/0006` 存在 | `test_maintenance_permission_is_not_inherited_from_no_show` | ✅ |
| 14 | 补充报告 §8.2 | ApprovalRecord/AuditEvent 等只追加 ORM 防旁路 | — | 7 张事实表自定义 Manager/QuerySet | `ImmutableFinancialFactManagerTests`（Payment/Refund/CashierShift/CashierShiftMovement bulk 拒绝）、`test_approval_request_facts_cannot_be_changed_through_save_or_queryset` | ✅ |
| 15 | 补充报告 §8.1 | 个人通知已读回执 | — | `core.0013` 迁移 + `NotificationRead`（notification×staff_profile 唯一约束） | `test_broadcast_read_receipt_is_personal_per_employee`、`test_mark_read_is_idempotent_and_keeps_first_read_at`、`test_read_all_scoped_to_my_audience` | ✅ |
| 16 | 实施记录 §6 | 旧财务入口退役（/finance/ 302、/finance_add/ 410） | — | 路由层 | `FinanceLegacyRouteRetirementTests`、`test_legacy_finance_page_redirects_to_single_pms2_entry`、`test_legacy_finance_write_endpoint_is_gone`、`apps/api/test_finance_route_regressions.py` | ✅ |
| 17 | 补充报告 §4 | 财务表单 DOM 脱离修复 + CSRF/夜审 DOM 模板守卫 | `bcf2bf8`/`71189f1`/`02fa499` | `templates/pms/billing_center.html` | `apps/api/test_center_template_regressions.py`（Session CSRF、禁止 document.cookie、表单替换前读值、夜审 Node append） | ✅ |
| 18 | 补充报告 §8.2 | 审计/夜审只读与执行权限分离（core.audit_view、core.night_audit_view） | — | `audit_center.py:174` core.audit_view；registry 236/248 | `test_audit_center.py` 权限四态、侧边栏可见性；`ddde637` 修正矩阵断言 | ✅ |
| 19 | 实施记录 §4.1 | 迁移清单（inventory.0006、core.0013、billing.0012-0019、0020） | — | 全部存在于 `apps/*/migrations/` | 迁移被全量测试套件执行 | ⚠️ G-03（清单缺 0021/0022） |
| 20 | 实施记录 §4.2 | 七条 MySQL 触发器（6 只追加 + 1 班次插入保护）与 check 命令 | `4b7a27a`/`f4f3da7` | `install_mysql_append_only_guards.py`/`check_mysql_append_only_guards.py` 各 7 条含名称/表/事件/时机/SIGNAL 校验；CI 用命令安装 | MySQL CI 步骤 install+check 全绿（API 核实） | ⚠️ G-01（手装 SQL 脚本界符缺陷） |
| 21 | 实施记录 §4.3/补充报告 §8.3 | MySQL 8 CI（migrate→全量测试→装 7 条触发器→检查） | `3a1445d`/`014716f` | `.github/workflows/ci.yml`（mysql:8.0 服务、四步顺序与声明一致） | GitHub API 核实：HEAD `00791aa` Django CI 与 tests(MySQL8) 双 success | ✅（G-04 备注） |
| 22 | 任务项 | e2e/五中心闭环覆盖整改后行为 | — | — | e2e 退款三方全流程、冲销审批流、班次现金、CSRF；closure 班次现金公式、关班差异、代关权限 | ✅（G-05 备注：退款过期仅 API/服务层覆盖） |

验证计数：**22 组声明，19 组确认一致，3 组登记缺口/备注（G-01、G-02、G-03）+ 2 项文档性备注（G-04、G-05）**。两份文档引用的全部 SHA（含补充报告 §4 的 9 个提交与 §8 基线 `dc7f9e2`）均在 git 历史中核实存在，且文件/范围与描述一致（如 `7363def` 确为 `apps/api/billing_center.py` 删除 9 行重复缓存失效）。

## 2. 财务域结论：**闭环**

退款申请→审批→执行→入账→过期→冲销全链路在服务层、模型层、数据库约束层三层实现，且与本轮新增的 `_validate_refund_approval_link` 一致：

- 审批/驳回/执行/定时到期四条路径统一走关联校验；错链（action_code/entity_type/entity_id/申请人/payment_id/amount/currency 任一不符）与畸形快照 fail closed，测试断言零 REFUND LedgerEntry 副作用。
- 通用审批入口（`POST /api/v1/approvals/request`）与退款流程的关系已收敛：权限前置（`d5485c4`）、同事务补建审批并关联 Refund（拒绝非 PENDING 审批记录、拒绝覆盖已有关联）、审批原因不强制相等（`fe89cf0`）——三处与 ApprovalActionRegistry（`billing.refund`/`billing.reversal` 的权限码、entity 校验器）完全一致。
- 班次现金公式、现金退款执行班次归属、关班差异原因、非负约束均落实到服务+模型+迁移；幂等重放（收款/押金/现金变动/退款执行）在关账后仍返回原事实。
- 与文档唯一偏差为 G-02（模型层班次归属校验缺专属负向测试），不影响链路闭环判定。

## 3. 权限域结论：**闭环**

- approval SoD：申请人≠审批人（通用入口 409 拦截）、退款三账号分离（模型+服务+API 三层）、冲销审批人≠执行人、代关班次需 `hotelapp.finance_access` 且记录实际关班人。
- 授权前置：通用审批 approve/reject 先做 `has_permission` 再进入退款路由守卫，权限不足返回 403（非 409），避免向无权者泄露业务路由语义；`test_approve_requires_registry_approve_permission` 固化该顺序。
- 权限码矩阵一致性：`ApprovalActionRegistry`（approve_permission）→ `apps/identity/registry.py` SPECIAL_PERMISSIONS（codename/note）→ 迁移组授权（billing.0008/0011/0013/0014/0020、inventory.0006、core.0010/0012）→ `BillingPermissionMigrationMatrixTests` 角色集合断言，四层核对无漂移。`core.audit_view`（店长/值班经理）与 `hotelapp.export_approve`（privacy.export_staff 专用动作，api_requestable=False）语义独立、测试覆盖四态。
- 维修权限与未到处理权限解耦有迁移纠正 + 矩阵测试双保险。

## 4. 数据库域结论：**代码与 CI 闭环；手装部署脚本存在缺陷（G-01）**

- 七条触发器清单（名称/表/事件/时机）：
  1. `trg_core_auditevent_no_update`（core_auditevent，UPDATE，BEFORE）
  2. `trg_core_auditevent_no_delete`（core_auditevent，DELETE，BEFORE）
  3. `trg_billing_ledgerentry_no_update`（billing_ledgerentry，UPDATE，BEFORE）
  4. `trg_billing_ledgerentry_no_delete`（billing_ledgerentry，DELETE，BEFORE）
  5. `trg_billing_shiftmovement_no_update`（billing_cashiershiftmovement，UPDATE，BEFORE）
  6. `trg_billing_shiftmovement_no_delete`（billing_cashiershiftmovement，DELETE，BEFORE）
  7. `trg_billing_shiftmovement_open_shift_insert`（billing_cashiershiftmovement，INSERT，BEFORE，校验 OPEN 班次 + 本人）
- `check_mysql_append_only_guards` 逐条核对名称/表/事件/时机/`SIGNAL SQLSTATE '45000'`，第 7 条额外核对 `FOR UPDATE`/`NEW.shift_id`/`NEW.created_by_id`/'OPEN'，与声明一致。
- MySQL 8 CI（`.github/workflows/ci.yml`）：migrate → 全量测试 → 安装 7 条触发器 → 检查，顺序与实施记录 §4.3 完全一致。经 GitHub 公共 API 核实：HEAD `00791aa` 上 Django CI（run 42235432445）与 tests/MySQL8（run 42235432446 同批）均 success；补充报告记录的 `9b982b0` 成功样本（852 tests）属实。
- **G-01**：`apps/core/sql/audit_immutability.sql` 第 63、82 行触发器结束符为 `END$`（单 `$`），正确应为 `END$$`。以 mysql 客户端直接执行该脚本（部署说明推荐的 DBA 路径）时，`trg_billing_shiftmovement_no_delete` 与 `trg_billing_shiftmovement_open_shift_insert` 两条语句永远无法按 `$$` 界符终止，触发器（含关班/非本人班次插入保护）不会被创建。`install_mysql_append_only_guards` 命令自行拼装语句、不解析该文件，故 CI 与命令安装路径不受影响——这正是 CI 全绿而脚本带病的原因。
- 文档自身声明的未决边界（生产触发器 DBA 部署、本地 MySQL 凭据、备份恢复演练）与代码状态一致，不属于本报告缺口。

## 5. 业务闭环域结论：**闭环（覆盖面备注 G-05）**

- `apps/api/test_e2e_flows.py`：`test_refund_request_pends_for_approval_then_owner_completes` 覆盖整改后完整退款链路（前台申请 403 拒自批→店长审批→财务执行→执行班次现金不足先补备用金→幂等重放不重复入账→执行班次与原收款班次分别按新口径关账、DTO 输出三方事实与 legacy_workflow）；`test_reverse_entry_posts_paired_reversal_and_audit` 覆盖"财务申请→店长审批→财务执行"冲销新流程。
- `apps/api/test_five_centers_closure.py`：`CashierShiftClosureTests` 覆盖班次现金公式、开班幂等、跨收银员关班拒绝、权限与物业隔离；`ReconciliationConsistencyTests` 覆盖退款周期账实一致与金额封顶。
- 模板/CSRF/DOM 回归守卫（`test_center_template_regressions.py`）与个人通知回执隔离测试覆盖补充报告 P0-02/P1-01/P1-05 整改。
- **G-05（备注，非缺陷）**：退款审批到期（`expire_due_refunds`/定时命令）与通用审批入口补建审批的兼容路径仅由 API/服务层测试（`test_billing_center.py`、`test_approval.py`）覆盖，e2e/五中心闭环测试未纳入；文档 §7 的测试清单也未声称 e2e 覆盖，故不算声明失实，登记为后续 E2E-01（浏览器端到端）扩展范围的一部分。

## 6. 缺口登记（只登记不修）

| 编号 | 等级 | 类型 | 缺口描述 | 证据 |
|---|---|---|---|---|
| G-01 | 中（部署物缺陷） | 代码缺陷 | `apps/core/sql/audit_immutability.sql` 两条触发器结束符 `END$` 应为 `END$$`（第 63 行 `trg_billing_shiftmovement_no_delete`、第 82 行 `trg_billing_shiftmovement_open_shift_insert`）；DBA 手工执行该脚本无法创建这两条触发器，其中一条正是关班/非本人班次的现金变动插入保护。`install_mysql_append_only_guards` 命令路径不受影响，MySQL CI 因此未暴露 | 文件 L49-L82（`cat -A` 确认 `END$^M$`）；`.github/workflows/ci.yml` L61-65 用命令安装；`install_mysql_append_only_guards.py` 独立拼装语句 |
| G-02 | 低 | 测试缺口 | `4bbe16e` 新增的模型层校验（现金退款 SUCCEEDED 必须关联执行人本人本物业班次；非现金退款不得伪造 execution_shift）无任何专属负向测试；两条 ValidationError 文案全仓库仅存在于 models.py，测试只覆盖正向路径（e2e 以执行人本人班次成功执行）与服务层"未开班拒绝" | `apps/billing/models.py:619-634`；全测试目录 grep 无断言；`4bbe16e` 仅改 models.py（+13/-2） |
| G-03 | 低 | 文档与代码不一致 | 实施记录 §4.1 迁移清单止于 billing.0019（§9.3 提及 0020），未登记已在 main 的 `0021_refund_execution_fact_freeze`（59ade32）与 `0022_refund_approval_expiry`（17b7fc7），清单与"最新 main"口径脱节 | `apps/billing/migrations/` 目录列表；git log -A 归属提交 |
| G-04 | 低 | 文档证据滞后 | 实施 §10.3/§8.5 要求以最新 main SHA 重新确认 CI 并回写结论；文档记录的 CI 成功样本止于 `9b982b0`/`c7f1224`（早于 600d0b9..fe89cf0 修复链）。经 GitHub API 核实，`600d0b9`→`aa577a5` 连续 8 个提交 Django CI 与 MySQL tests 双红（与 §10.4 自述的通用审批回归冲突吻合），自 `fe89cf0` 起恢复，HEAD `00791aa` 双工作流 success——实际现状已绿，但文档未回写最终 SHA 结论 | GitHub API runs 列表（fe89cf0/8eea291/00791aa success；600d0b9..aa577a5 failure）；`79cc359` 运行 42234478997 失败步骤为 Run tests |
| G-05 | 备注 | 覆盖面 | 退款审批到期与通用审批兼容路径未进入 e2e/五中心闭环测试（详见 §5） | `test_e2e_flows.py`/`test_five_centers_closure.py` 无 expire/refund-generic 用例 |

## 7. 测试与 CI 数字

- 本地（本次核查执行，sqlite）：`DJANGO_ENV=test DJANGO_DB_ENGINE=sqlite manage.py test --noinput` → **Ran 853 tests in 244.898s，OK**，与验收基线 853 项一致，无回归。
- GitHub Actions（公共 API 核实，2026-11-06）：HEAD `00791aa` → Django CI **success**（run 42235432445）、tests/MySQL 8 **success**；补充报告引用的 `9b982b0` MySQL8 成功样本（852 tests / 7 条触发器安装检查通过）属实；`fe89cf0` 起 CI 恢复双绿。
- 本报告为验收 §15 证据包的核查依据之一；G-01 修复前，生产 MySQL 验收不得依赖 `apps/core/sql/audit_immutability.sql` 手工执行路径。

---

## 复核（2026-11-06，主线程）

| 缺口 | 处置 | 状态 |
|---|---|---|
| G-01 | audit_immutability.sql 63/82 行 `END$` → `END$$`（2 处），手工部署路径恢复完整 7 触发器 | 已修复 |
| G-02 | 新增 `CashRefundShiftOwnershipTests`（apps/billing/test_whitebox_services.py）：他人 OPEN 班次借班执行 → 拒绝且退款停留 APPROVED、零账本副作用；执行人本人班次 → 正常执行（归属语义正反双验证） | 已修复 |
| G-03 | 整改实施记录迁移清单补 billing.0021/0022 | 已修复 |
| G-04 | CI 双绿已由 fe89cf0 起恢复（GitHub API 核实）；HEAD CI 双绿 | 已确认（文档回写由并行流跟进） |
| G-05 | 退款到期/通用审批兼容路径纳入 e2e——登记 Phase 4 契约批次 | 登记待排期 |

全量回归：854 项测试通过（SQLite；含本批复核新增 1 项）。
