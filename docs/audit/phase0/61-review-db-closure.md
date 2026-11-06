# 61 · PMS2 数据库迁移与业务闭环域审查报告（code-review + tech-debt-audit，基线 1a39dd0 以来并行提交）

- 审查对象：① `git diff 1a39dd0..HEAD -- "*/migrations/*"`（15 个迁移文件 / 22 个迁移提交）；② `apps/reservations`、`apps/api/front_desk.py`、`apps/api/test_e2e_flows.py`、`apps/api/night_audit.py`、`apps/billing/night_audit.py`、`apps/billing/services_v2.py`、`apps/inventory/services.py` 的业务闭环变更（28 个提交）
- 方法：code-review 四维扫描（安全 / 正确性 / 契约 / 性能）+ tech-debt-audit 快扫；缺陷先失败测试后修复
- 审查时点：main HEAD `bf4e826`（已 `git pull`，工作树仅含本报告修复）
- 测试口径：`DJANGO_ENV=test DJANGO_DB_ENGINE=sqlite`（MySQL 权限问题按任务约定由用户处理）；并发缺陷以锁序走查 + 白盒方式覆盖
- 基线全量测试：**861 全过**；修复后全量：见 §6

## 0. 结论速览

- **迁移**：15 个迁移文件逐一判定**全部通过**（§1）：依赖顺序正确、约束/索引合理、data migration 幂等且反向显式登记、无删表/删列/改类型破坏性变更、`makemigrations --check --dry-run` 无漂移（修复前后各验证一次）。
- **正确性**：发现 **3 个缺陷并全部修复**（§3）——1 个 P1 营业日重开被终态审批**永久阻挡**（重开闭环断链）、1 个 P2 已批准重开审批可在营业日再次关账后**重放驱动第二次重开**、1 个 P3 在住换房**双向并发 ABBA 死锁窗口**。P0 = 0。
- **重点核查三项**（§4）：营业日阻挡（发现 D-01/D-02 并修复）；维修停用同事务库存同步（实现正确但**零直接测试覆盖**，补 4 项服务级测试）；押金回减行锁（实现正确，锁序无反向）。
- **技术债**：登记 5 项（§5），不顺手大改。
- **本报告修复**：全部为服务层代码，**不需要新模型 / 新 migration**（登记：无迁移变更）。

## 1. 迁移审查清单（逐文件判定）

| # | 迁移文件 | 判定 | 审查要点 |
|---|---|---|---|
| 1 | billing/0012_refund_execution_and_shift_cash_movements | 通过 | 退款三段字段（executed_by/at、reference、幂等键 unique、execution_shift）+ Payment.cashier_shift（PROTECT、可空）+ CashierShiftMovement 建模；金额 >0 CHECK、(shift, idempotency_key) 唯一；依赖 0011 正确 |
| 2 | billing/0013_grant_refund_execution_and_cash_movement_permissions | 通过 | 权限 get_or_create + 财务组授权；revoke 对称摘除全部组 |
| 3 | billing/0014_cashier_shift_manage_permission | 通过 | Meta permissions + 前台/财务/值班经理/店长四角色授权，回收对称 |
| 4 | billing/0015_alter_cashiershiftmovement_id | 通过 | AutoField→BigAutoField（放宽型，非破坏）；与 models.py 一致 |
| 5 | billing/0016_refund_sod_state_constraints | 通过 | 数据迁移只把无法证明三方分离的历史记录标 `legacy_workflow`，不改写金额/状态/人员/账本事实；5 条 CHECK（3 条 SoD + 审批事实 + 执行事实）；反向保留标记（审计事实），幂等（update 口径） |
| 6 | billing/0017_cash_shift_movement_direction_constraint | 通过 | direction IN/OUT CHECK，表单 choices 之外的数据库兜底 |
| 7 | billing/0018_cashier_shift_nonnegative_cash_constraints | 通过 | 前置 RunPython 对负数现金事实 **fail-fast**（RuntimeError 阻断迁移并列出异常班次 ID），禁止静默修正账务；三条非负 CHECK；反向 noop |
| 8 | billing/0019_refund_requires_approval_record | 通过 | 非待审批退款必须关联审批记录 CHECK；无法追溯历史标 legacy，不补造事实 |
| 9 | billing/0020_reversal_approval_sod | 通过 | 红字冲销迁移为财务申请/执行 + 值班经理/店长审批；`_base_manager` 绕过不可变账本管理器回填 legacy 标记（ba687ba）；授权函数正/反向对称（反向恢复旧权限名与旧授权集合）；ck_ledger_reversal_approval_link 三分支（历史/新冲销/普通凭证）覆盖完整；依赖含 auth/contenttypes/hotelapp 正确 |
| 10 | billing/0021_refund_execution_fact_freeze | 通过 | processed_at 状态事实 + 执行参考号冻结 CHECK；历史不满足者标 legacy 不补造 |
| 11 | billing/0022_refund_approval_expiry | 通过 | 先 Remove 3 条旧约束 → AlterField 增加 EXPIRED 状态 → RunPython 同步"审批已过期但退款仍 PENDING"的存量（processed_at 取审批真实 approved_at，不伪造；无 approved_at 标 legacy）→ 按支付当前事实重算 REFUND_PENDING/REFUNDED/PARTIALLY_REFUNDED/SUCCEEDED → 重加 3 条含 EXPIRED 分支的 CHECK；反向 no-op 保留到期事实 |
| 12 | core/0012_night_audit_view_permission | 通过 | 夜审只读权从执行权组**复制**（不扩权）、Add-only 反向不撤销；依赖 core 0011 + hotelapp 0020 正确 |
| 13 | core/0013_notification_per_user_read_receipts | 通过 | NotificationRead（(notification, staff_profile) 唯一 + 员工时间索引）+ Notification.resolved_at + 物业/解决时间索引；数据迁移三分支保守正确：待办全局已读→resolved_at、定向通知→个人回执、**岗位广播不猜测接收人不造回执**；反向保留新回执事实 |
| 14 | inventory/0005_housekeeping_permission_codes | 通过（有后续修正） | 清洁/维修三权从旧权限组复制，Add-only；其中 maintenance_manage 映射自 no_show_handle 属历史污染，由 0006 立即纠正（见 TD-61-02） |
| 15 | inventory/0006_align_maintenance_manage_role_grants | 通过 | 清除 0005 错误复制后按角色矩阵仅授值班经理/店长；反向不恢复污染 |

通用判定：无破坏性变更（无删表/删列/字段类型收窄）；所有 data migration 正向幂等（update / get_or_create / 显式 guarded），反向要么对称恢复（权限类）要么显式 no-op 并注明理由（事实保留类）；`DJANGO_ENV=test DJANGO_DB_ENGINE=sqlite manage.py makemigrations --check --dry-run` = **No changes detected**。

## 2. 审查提交清单

- 迁移提交 22 个：`cc438ff`、`2a000f8`、`613570c`、`fa9b418`、`311638a`、`4fef412`、`da1a55e`、`90f30e6`、`21f7feb`、`11b9fd8`、`e21ab9f`、`de7d90e`、`e62f6e3`、`c4af2ab`、`99c2cd4`、`324bf82`、`150aaff`、`ba687ba`、`e4a9573`、`c05d716`、`59ade32`、`17b7fc7`
- 业务闭环提交 28 个（reservations / front_desk / e2e / night_audit / inventory services）：退款三方分离与经理驳回端点（`26e1331`、`c138a94`、`4a344ee`、`59d5303`、`5ffbc8f` 及 10 个配套测试提交）、多账单汇总与跨币种（`3d1ec73`、`8b09341`）、确认/入住状态分离（`6fe2fb2`）、清洁维修权限拆分（`628e438`）、夜审阻断清单（`19a4b53`、`58b7194`）等

## 3. 缺陷清单（全部已修复，先失败后通过）

### FD-61-01（P1 · 正确性/闭环）终态审批把营业日重开申请永久阻挡

- **位置**：`apps/billing/services_v2.py` `ApprovalService.request_approval`（修复前 51-79 行）
- **根因**：审批幂等键 = sha256(物业:动作:实体:申请人)，**不含状态**；`request_approval` 对"同键已存在记录"一律返回既有记录。重开申请被驳回（REJECTED）或审批超时（EXPIRED）后，同一申请人再次提交 `request_reopen` 永远拿回那条终态记录，`approve_reopen` 又只接受 APPROVED——营业日阻挡**永远无法通过重新申请解除**，闭环基线 §4.2"符合条件时双人申请重开"断链（UI 还会把终态记录误报为"重开申请已提交"）。同一缺陷影响所有走 `ApprovalService` 的高危动作（含红字冲销被驳回后的重新申请）。
- **修复**：同键记录为 PENDING/APPROVED 时维持幂等重放契约不变；终态（REJECTED/EXPIRED/CANCELLED）不再阻挡——若同范围已有待审批申请先幂等返回（防堆叠），否则以"原键:retry:序号"派生新键创建新申请；并发撞键由唯一约束仲裁（PENDING 幂等返回、终态撞键换序号重试，上限 8 次）。
- **测试**：`apps/billing/test_night_audit_reopen.py` 驳回后重申→新审批→批准→REOPENED；过期后重申；CANCELLED 后重申；PENDING 重放契约不变 ×2。修复前 5 项失败，修复后全过。

### FD-61-02（P2 · 安全/正确性）已决策的重开审批可重放驱动第二次重开

- **位置**：`apps/billing/night_audit.py` `NightAuditService.approve_reopen`
- **根因**：原实现接受状态为 APPROVED 的历史审批（`if approval.status == PENDING: approve()` 后仅校验"状态=APPROVED 且审批人=当前人"）。营业日重开→再次夜审关账后，旧 APPROVED 审批记录可直接再次驱动 `approve_reopen`——重开动作没有对应的新申请事实，审批留痕与实际重开不一一对应。
- **修复**：`approve_reopen` 只接受 PENDING 审批（本次调用内完成批准）；非 PENDING 一律拒绝并提示重新提交申请。夜审专用审批端点即批准入口，行为与闭环文档一致；两步流（通用审批端点先批 + approve_reopen 再执行）为无测试覆盖的冗余路径，随本修复关闭（登记于 §5 TD-61-04）。
- **测试**：同文件 `test_stale_approved_reopen_approval_cannot_replay_after_reclose`——重开后再关账，旧审批 id 驱动 approve_reopen 必须抛 NightAuditError 且营业日保持 CLOSED。修复前失败，修复后通过。

### FD-61-03（P3 · 性能/可用性）在住换房双向并发 ABBA 死锁窗口

- **位置**：`apps/reservations/lifecycle_services.py` `change_stay_room`
- **根因**：目标房间与源房间分两次 `select_for_update` 加锁。A→B 与 B→A 两个换房事务并发时，T1 持 B 等 A、T2 持 A 等 B，形成 ABBA 死锁（MySQL 立即侦测回滚并报错，SQLite 串行写不可复现）。
- **修复**：两间房按主键升序**一次查询**统一加行锁（`filter(pk__in=sorted_ids).in_bulk()`），全局锁序一致消除死锁窗口；校验语义不变（目标房必须存在/启用/同房型/空闲净房）。
- **测试**：既有换房测试（含跨物业 404、同房幂等、房间占用拒绝）全量回归通过；锁序修复以代码走查 + 回归覆盖（SQLite 无法确定性复现 ABBA）。

## 4. 三个重点核查项结论

| 核查项 | 结论 |
|---|---|
| 营业日阻挡（拒绝后可重开） | **发现 2 缺陷并修复**（FD-61-01/02）。修复后闭环：申请→驳回/超时→同一申请人可重新申请（新审批事实）→另一审批人批准→重开；待审批申请幂等重放不变；旧终态审批不能重放。 |
| 维修停用同事务库存同步 | **实现正确**：`_sync_inventory_out_of_service` 在 `change_maintenance_status` 同一事务内逐行 `select_for_update` 同步 `stay_date >= 物业营业日` 的 out_of_service（+1/-1、version+1、负数封 0），容量不足（total < held+sold+out_of_service）整体失败回滚房间状态（fail-closed），历史行不回写；恢复不联动清洁状态。OUT_OF_ORDER↔OUT_OF_SERVICE 互换 delta=0 不重复累计。**缺口：零直接测试覆盖**——补 4 项服务级测试（未来行同步、fail-closed 回滚、封 0 恢复、级别互换不双计），全过。 |
| 押金回减行锁 | **实现正确**：`record_deposit` 在 Reservation 行锁下当前读累加（≤房费总额上限）；`execute_refund` 对押金类支付回减前 `Reservation.objects.select_for_update()` 当前读锁定该行（与累加互不覆盖）；全局锁序 Payment→Refund→Reservation 单向，全仓无"先锁 Reservation 再锁 Payment"的反向路径，无死锁环。 |

## 5. 技术债登记（tech-debt-audit 快扫，不顺手大改）

| 编号 | 等级 | 位置 | 内容与建议 |
|---|---|---|---|
| TD-61-01 | 低 | `apps/reservations/front_desk_services.py`、`apps/reservations/lifecycle_services.py`、`apps/billing/services_v2.py`、`apps/inventory/services.py`、`apps/billing/night_audit.py` | `_money` / `_property_today`（物业时区当日）/ 物业日历金额辅助函数 5 处重复实现，口径一致但需人工同步；建议收敛到共享模块并保留原入口别名。 |
| TD-61-02 | 低 | `apps/inventory/migrations/0005`、`0006` | "先污染后修正"迁移对：0005 把 no_show_handle 组授权复制到 maintenance_manage，0006 立即清除重授。历史不可压缩；后续新专项权限应直接按正式角色矩阵授予，不再复制旧权限组。 |
| TD-61-03 | 中 | `apps/billing/services_v2.py` `financial_summary` | 全表载入内存聚合；Payment/Refund 以 `created_at__date` 过滤（无法命中索引，且按 settings 时区而非物业时区取日期）。报表中心已有独立口径，此函数属基线遗留，复用前需先修口径。 |
| TD-61-04 | 低 | `apps/billing/services_v2.py` `request_approval` | 终态重试键派生依赖同范围记录计数（每次重申 2-3 个查询）；量大后可为 (property, action_code, entity_type, entity_id, requested_by, status) 建部分索引。另：`approve_reopen` 关闭两步流后，通用审批端点对 night_audit.reopen 仅剩列表/详情用途，后续可评估在注册表层面收敛。 |
| TD-61-05 | 低 | `apps/api/front_desk.py` `_folio_summaries` | 列表页对每张账单 prefetch 全量 entries+payments 后内存聚合，凭证量线性增长；当前规模可接受，超阈值时改聚合查询。 |

## 6. 修复交付与测试结果

- **修复代码**（3 处，无新模型/迁移）：
  - `apps/billing/services_v2.py`（ApprovalService 终态重申）
  - `apps/billing/night_audit.py`（approve_reopen 仅接受 PENDING）
  - `apps/reservations/lifecycle_services.py`（换房按主键升序统一行锁）
- **新增测试 11 项**：
  - `apps/billing/test_night_audit_reopen.py`（新文件，7 项：5 项缺陷驱动先失败 + 2 项契约保持）
  - `apps/inventory/test_whitebox_services.py`（追加 MaintenanceInventorySyncTests，4 项）
- **测试结果**：
  - 修复后相关域（apps.reservations + apps.api.test_e2e_flows + apps.inventory + apps.core + apps.billing）：139 全过（不含后补 inventory 4 项）
  - 全量 `manage.py test --noinput`：**868 全过**（基线 861 + 重开回归 7；加入 4 项库存同步测试后终验见下）
  - 终验（含全部 11 项新测试）：**872 全过，OK，0 失败 0 错误**
  - `makemigrations --check --dry-run`：No changes detected（修复前后各一次）

## 7. 闭环链路核查总结论

- **预订→入住→退房→清洁链**：通过。锁序 Stay→ReservationRoom→Reservation→Folio→Room 全局单向；退房置脏触发清洁链，DIRTY→CLEANING→INSPECTING→CLEAN 链式规则 + 净房放行三层复核（API/服务/权限）成立；检查不通过返工（INSPECTING→DIRTY）闭环覆盖。
- **维修链**：通过（FD-61-03 修复换房并发；维修停用↔库存同步同事务 fail-closed 成立，测试覆盖补齐）。排房/换房阻断停用房（is_sellable_now 门禁）与恢复后清洁独立判断均验证。
- **财务→审批→夜审链**：修复后通过。退款申请/审批/驳回/执行/到期五路径共享 Payment→Refund 锁序与 `_validate_refund_approval_link` 事实校验；审批幂等键终态不再卡死重开闭环；夜审过租按 reference 幂等、REOPENED→再夜审→CLOSED 闭环成立；break-glass 限时单次执行 + 独立复核成立。
- **押金/现金班次链**：通过。押金收/退双向行锁闭环；现金退款班次归属（执行人本人 OPEN 班次 + 抽足余额）与关班对账（Shift→Payment→Refund 锁序、当前读汇总）成立。
