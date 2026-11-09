# 45 · 白盒服务层深度单元测试审查报告（子智能体 1）

- 范围：业务服务层逐函数白盒审查 + 缺陷修复 + 针对性单元测试
- 分支：main（审查起点 `git pull` 后与 origin 一致，基线 729 项测试全过）
- 结论：**确认缺陷 14 项（P1×2、P2×12），全部修复；新增针对性测试 24 项；全量回归 753 项通过**（729 基线 + 24 新增；修复后共 6 次全量运行，前 1 次含 1 个与本报告无关的已知 Windows 时钟粒度偶发错误，随后连续 4 次 OK，见 §5.2）
- 交付文件：
  - 修复：`apps/billing/services_v2.py`、`apps/reservations/front_desk_services.py`、`apps/reservations/lifecycle_services.py`、`apps/guests/services.py`、`apps/inventory/services.py`
  - 新增测试：`apps/billing/test_whitebox_services.py`（12）、`apps/reservations/test_whitebox_services.py`（6）、`apps/guests/test_whitebox_services.py`（3）、`apps/inventory/test_whitebox_services.py`（3）
- 纪律：未改 API 层/模板/URL/registry/migration；未做业务规则变更；未删除既有测试。

## 1. 方法

1. 逐文件逐函数通读 11 个在册服务文件，按白盒清单七类（金额精度/状态机/幂等/边界/事务/授权/并发）逐项核对。
2. 每个疑似缺陷先写**确定性复现测试**（并发竞态用"预检查询首查扑空 + 数据库唯一约束兜底"的方式模拟，时区缺陷用固定 `timezone.now()` + 太平洋/基里蒂巴蒂（UTC+14）物业模拟），确认在修复前失败、修复后通过。
3. 修复最小化：只动服务层；不改变对外 API 契约（响应字段、状态码、错误语义均不变）。

## 2. 已修复缺陷清单

| 编号 | 等级 | 位置 | 缺陷 | 根因 | 修复 | 测试 |
|---|---|---|---|---|---|---|
| WB-01 | P2 | `billing/services_v2.py::_money`、`reservations/front_desk_services.py::_money`、`reservations/lifecycle_services.py::_money`、`inventory/services.py::adjust_daily_rate`（4 处同类） | 金额为超出 Decimal 上下文精度的超大有限值（如 `1e30`）时，`quantize` 抛未捕获 `decimal.InvalidOperation`，服务层以 500 崩溃而非业务校验错误 | 只对 `Decimal()` 构造做了 try/except，`quantize` 调用未防护 | 4 处统一在 `quantize` 外捕获 `InvalidOperation` → 转为各自的 `ValueError/FrontDeskOperationError/LifecycleOperationError` | `PostEntryMoneyGuardTests::test_post_entry_rejects_unquantizable_huge_amount`、`MoneyBoundaryTests::test_money_rejects_non_finite_and_oversized_values`、`reservations/MoneyBoundaryTests`×2、`inventory/AdjustDailyRateBoundaryTests::test_adjust_daily_rate_rejects_unquantizable_price` |
| WB-02 | P2 | `billing/services_v2.py::BillingService.reverse_entry` | 红字冲销凭证 `business_date=timezone.localdate()` 取默认时区日期，与其它账务写入口（`_business_date`，物业时区）不一致；跨时区物业在默认时区跨零点前后冲销会记入错误营业日，污染 `financial_summary` 按营业日聚合 | 复制粘贴旧 `services.reverse_ledger_entry` 口径未同步时区修复 | 改为 `BillingService._business_date(original.property)` | `ReverseEntryBusinessDateTests::test_reverse_entry_uses_property_business_date`（固定 now + UTC+14 物业，断言冲销日=物业当日） |
| WB-03 | P2 | `billing/services_v2.py::ApprovalService.request_approval` | 并发重复审批申请同时通过幂等预检后，撞 `idempotency_key` 唯一约束直接抛 `IntegrityError`（500），违背"同 key 幂等返回"契约 | check-then-act 无约束仲裁兜底转换 | create 包裹嵌套 atomic（savepoint），捕获 `IntegrityError` 后按 key 重查并幂等返回 `(existing, False)` | `ApprovalRaceTests::test_request_approval_race_falls_back_to_existing_record` |
| WB-04 | P2 | `billing/services_v2.py::BillingService.post_entry` | 并发同凭证号（reference）入账撞唯一约束抛 `IntegrityError`，而非幂等返回既有凭证 | 同上 | 同上；幂等事实比对抽取为 `_entry_facts_match` 供预检与撞键两条路径共用，事实不一致仍拒绝覆盖 | `PostEntryMoneyGuardTests::test_post_entry_duplicate_reference_race_is_idempotent` |
| WB-05a | P1 | `billing/services_v2.py::BillingService.record_deposit` | 押金上限校验 `deposit_amount + amount > total_amount` 读取的是**传入实例的脏快照**；并发押金或陈旧实例可绕过"累计押金 ≤ 房费总额"上限且 `save` 回写丢失并发写入（丢失更新） | 未对预订行加锁重读（先查后写竞态） | 校验前 `Reservation.objects.select_for_update().get(pk=...)` 行锁下重读最新值 | `DepositGuardTests::test_record_deposit_rechecks_cap_on_fresh_lock`（DB 已有 60 押金、传入实例仍为 0，请求 50 必须拒绝且不覆盖） |
| WB-05b | P2 | 同上 | 押金账单选择 `filter(status=OPEN).first()` 未限定 `folio_type=MASTER`；Folio `Meta.ordering=(-opened_at,)` 下更晚开立的非主账单会被选中，押金与 DEPOSIT 凭证入错账单（错误信息与 collect_payment 语义均指向主账单） | 遗漏类型过滤 | 增加 `folio_type=Folio.FolioType.MASTER` | `DepositGuardTests::test_record_deposit_targets_master_folio` |
| WB-06 | P1 | `reservations/front_desk_services.py::check_out_stay` | 退房账单选择 `filter(property, reservation).first()` 未限定主账单：存在更晚开立的 OPEN 分账单时，房费凭证、余额校验、关账全部落在错误账单上——主账单余额未结清也可能退房（资金风险），或主账单该关不关 | 同上 | 增加 `folio_type=Folio.FolioType.MASTER` | `CheckOutFolioScopeTests::test_check_out_settles_master_folio`（修复前主账单 OPEN、分账单被误关闭） |
| WB-07 | P2 | `reservations/lifecycle_services.py::_folio_for_update` | 续住/换房的账单状态门禁未限定主账单，分账单 OPEN 可掩盖已关闭的主账单（主账单已结仍可续住） | 同上 | `_folio_for_update` 增加 `folio_type=MASTER` | `LifecycleFolioScopeTests::test_extend_stay_requires_open_master_folio` |
| WB-08 | P2 | `billing/services_v2.py::BillingService.post_entry` | 不校验凭证币种与账单币种一致，异币凭证可入同一账单；而余额/应收逻辑对条目直接求和，造成跨币种金额相加（违反"多币种不相加"） | 缺少币种一致性守卫 | `currency != folio.currency` 直接拒绝 | `PostEntryMoneyGuardTests::test_post_entry_rejects_foreign_currency` |
| WB-09 | P2 | `guests/services.py::GuestService.create_guest` | Guest 与 GuestPhone 两次写入无事务包裹，电话写失败遗留孤儿客人档案 | 缺 `@transaction.atomic` | 加服务级事务 | `CreateGuestAtomicityTests::test_create_guest_rolls_back_when_phone_write_fails` |
| WB-10 | P2 | `guests/services.py::_mask` | 长度 <6 的值（短电话/短证件号）"脱敏"后原样写入审计 `after_json`（明文落审计） | 短值分支直接 return 原文 | 短值一律全打码 `*`；≥6 字符保持既有口径不变 | `MaskDisciplineTests`×2 |
| WB-11 | P2 | `billing/services_v2.py::BillingService.open_shift` | 并发开班撞 `open_shift_key` 唯一约束抛 `IntegrityError`，而非幂等返回既有班次 | 同 WB-03 | 嵌套 atomic + `IntegrityError` 重查幂等返回 | `OpenShiftRaceTests::test_open_shift_race_returns_existing_shift` |
| WB-12 | P2 | `inventory/services.py::housekeeping_summary` | "今日流转"窗口 `make_aware(combine(today, min))` 用 settings.TIME_ZONE 解释**物业日历日期**的零点，跨时区物业把昨天的流转错记进"今天"（或漏记） | 与 `_property_today` 的物业时区口径脱节 | `datetime.combine(today, min, tzinfo=物业时区)` | `HousekeepingSummaryTimezoneTests::test_today_window_uses_property_timezone`（UTC+14 物业 + 固定 now） |
| WB-13 | P2 | `billing/services_v2.py::BillingService.request_refund` | 并发同幂等键退款申请撞 `(property, idempotency_key)` 唯一约束抛 `IntegrityError` | 同 WB-03 | 嵌套 atomic + 撞键重查，事实一致幂等返回、不一致报"该幂等键已用于不同退款申请" | `RefundRaceTests::test_request_refund_race_returns_existing_refund` |
| WB-14 | P2 | `billing/services_v2.py::BillingService.collect_payment` | 并发同参考号收款撞 `provider_ref` 唯一约束抛 `IntegrityError`（Payment+LedgerEntry 一起回滚后 500） | 同 WB-03 | 嵌套 atomic + 撞键重查，六要素一致幂等返回既有支付 | `CollectPaymentRaceTests::test_collect_payment_race_returns_existing_payment` |

等级依据验收标准 §14：P0 资金/权限绕过（未发现可经 HTTP 边界利用的 P0）；P1 流程错误（WB-05a 押金上限绕过、WB-06 退房结错账单）；P2 边界错误。

## 3. 审查覆盖函数清单

| 文件 | 覆盖函数 | 结果 |
|---|---|---|
| apps/billing/services_v2.py | `ApprovalService.request_approval / approve / reject`；`BillingService._money / _business_date / post_entry / collect_payment / record_deposit / request_refund / complete_refund / reverse_entry / open_shift / close_shift / financial_summary` | WB-01/02/03/04/05/08/11/13/14 修复；`close_shift`、`financial_summary`、`approve` 见登记项 R-01/R-03/R-09 |
| apps/billing/approval_registry.py | `_is_sensitive_key / _is_sensitive_value`、`validate_refund_entity / validate_business_date_entity / validate_staff_export_entity`、`ApprovalActionRegistry.get / registered_codes / api_requestable_codes / approver_permissions / validate_entity_type / validate_payload` | 通过（18 位身份证正则、16+ 连续数字、payload 白名单/长度/float 拒绝均复核；无修复） |
| apps/reservations/services.py | `normalize_phone / _fingerprint / _get_rate_plan / _create_reservation / create_reservation / cancel_pending_reservation` | 通过；pending 上限竞态登记 R-04 |
| apps/reservations/front_desk_services.py | `_property_today / _money / _folio_balance / _check_room_assignment_conflict / assign_room / check_in_stay / collect_manual_payment / check_out_stay` | WB-01/06 修复；其余通过（入住营业日 CLOSED 门禁、Stay OneToOne 去重、余额边界复核） |
| apps/reservations/lifecycle_services.py | `extend_stay / change_stay_room / mark_reservation_room_no_show / receive_manual_deposit / request_refund / approve_refund` | WB-01/07 修复；No-Show 全单状态收敛与账单不关闭登记 R-11 |
| apps/inventory/services.py | `change_housekeeping_status / _sync_inventory_out_of_service / change_maintenance_status / housekeeping_summary / stay_dates / get_room_type_availability / hold / confirm / release_inventory_hold / release_sold_inventory_for_room_line / adjust_daily_rate` | WB-01/12 修复；其余通过（状态机转移表、库存 CheckConstraint 口径保护复核） |
| apps/guests/services.py | `doc_no_hash / encrypt / decrypt / register_document / _require_reveal_reason / reveal_document / reveal_phone / _mask / GuestService.create_guest / update_guest / add_phone / GuestMembershipService.*` | WB-09/10 修复；会员暂停/恢复状态机与唯一键复核通过 |
| apps/identity/services.py | `EmployeeLifecycleService.get_membership / active_assignments / create_employee / provision_account / bind_existing_user / assign_role / end_assignment / set_employment_status / reset_password / provision_invitation_account / activate_invitation_account` | 通过；R-05/R-06 登记（SoD 自我提权阻断、首登强制改密、ck 约束钳制均复核在位） |
| apps/billing/night_audit.py | `NightAuditService._resolve_date / preview / run / request_reopen / approve_reopen / request_break_glass / execute_break_glass / review_break_glass` | 通过；R-10 登记（RUNNING 残留阻塞）。夜审过租幂等 reference、双人审批 SoD、break-glass 单经理判定/10 分钟窗/事后复核复核通过 |
| apps/core/services_notifications.py | `property_business_date / _item / _collect_active_items / refresh_operational_todos` | 通过（upsert 对账、已读不重置、空 dedupe_key 不销项复核） |
| apps/core/rate_limit.py | `fingerprint / cleanup_expired / check / retry_after_seconds` | 通过（fail-closed、空维度不成共享桶、F() 原子自增复核；未知维度名不会 KeyError——`values.get(d)` 短路） |

## 4. 未修复登记项（不改 API/模型/业务规则，移交后续裁决）

| 编号 | 等级 | 位置 | 描述 | 不修原因 |
|---|---|---|---|---|
| R-01 | P1 | `billing/services_v2.py::close_shift` | `expected_cash = opening_cash`，未含班次内现金收支；与模型 help_text"开班备用金+现金收入−现金支出"矛盾。班次内收的现金全部被算作差异 | Payment/LedgerEntry 无收银员归属字段，精确计算需迁移；openapi_schema 明文锁定"当前口径"。需业务决策+迁移后统一修 |
| R-02 | P2 | `billing/services_v2.py::reverse_entry` | 服务层不校验凭证所属物业（无 property_obj 参数）；HTTP 入口已先按物业过滤（`apps/api/billing_center.py:634`），属纵深防御缺口 | 加参数即改服务签名并需 API 层联动传参（禁改 API 层）；现有边界由入口权限兜底 |
| R-03 | P2 | `billing/services_v2.py::financial_summary` | payment_total/refund_total/total_revenue 跨币种直接相加；payment_count/total 含 FAILED/PENDING 支付 | 修正需改变 API 响应口径（拆分币种/过滤状态属契约变更） |
| R-04 | P2 | `reservations/services.py::_create_reservation` | 同手机号 PENDING 上限检查为先查后插、无锁，并发可小幅超限（2→N） | 需按手机号哈希粒度锁或新约束，影响公开预订主路径，风险大于收益；上限本为营销风控软限制 |
| R-05 | P3 | `identity/services.py::create_employee / provision_account` | 用户名唯一性先查后建，并发撞名 → IntegrityError 500（DB 唯一约束兜底在位，仅缺幂等转换） | 低频管理操作，500 可接受；修复模式同 WB-03，待统一处理 |
| R-06 | P3 | `identity/services.py::assign_role` | `starts_at` 晚于当前的授权不参与重复判定，可对同组创建多条重叠的未来授权 | 未来生效授权的并集语义可能是有意支持（预约调岗），需业务确认 |
| R-07 | P3 | `billing/services_v2.py::collect_payment` | 已占用参考号的 FAILED 支付永久阻塞同号重放（报"已用于不同支付"） | 属幂等键生命周期策略，需产品口径 |
| R-08 | P3 | `billing/services_v2.py::ApprovalService.request_approval` | 幂等键含 requested_by：同实体同动作可被不同申请人各建一条 PENDING 审批 | 是否允许多人分别申请属双人控制语义决策 |
| R-09 | P3 | `billing/services_v2.py::ApprovalService.approve` | 过期分支写 `approved_at` 不写 `approved_by`，审计字段口径不一致 | 纯记录口径，不涉及行为 |
| R-10 | P3 | `billing/night_audit.py::NightAuditService.run` | 进程崩溃遗留 RUNNING 夜审记录会永久阻塞该营业日重跑，需人工清理 | 需超时回收策略（业务决策）；当前 fail-closed 是安全方向 |
| R-11 | P3 | `reservations/lifecycle_services.py::mark_reservation_room_no_show` | 全部明细 No-Show 后预订置 NO_SHOW，但主账单不关闭、不产生费用/没收动作 | 与文档口径一致（"未到候选只列示，不自动没收押金"），属已披露的首期边界 |
| R-12 | P3 | `guests/services.py::GuestService.update_guest` | 全字段 `save()` 无 update_fields，并发编辑有丢失更新窗口 | 低频管理操作；引入字段级更新需逐调用点核对 |

## 5. 回归与稳定性

### 5.1 测试数字

- 修复前针对性复现：新增 24 项测试中 19 项按预期失败（其余 5 项为同文件回归/边界补充），失败签名与 §2 根因一一对应（`IntegrityError`×5、`InvalidOperation`×4、`ValueError not raised`×2、账单归属断言×3、时区断言×2、脱敏断言×3 等）。
- 修复后：24 项全部通过。
- 全量：`DJANGO_ENV=test DJANGO_DB_ENGINE=sqlite manage.py test --noinput` → **Ran 753 tests, OK**（修复后 6 次全量运行，最后连续 4 次 OK）。

### 5.2 已知偶发（与本报告改动无关）

- 修复后 6 次全量运行中的第 1 次出现 1 个不可复现 error（无法捕获用例名）；`identity/services.py::end_assignment` 代码注释已记载该 Windows 时钟粒度导致的"全量回归偶发 IntegrityError"既有问题（15.6ms tick 内 ends_at==starts_at，已被钳制逻辑收敛为偶发）。随后连续 4 次全量 OK。

### 5.3 兼容性核对

- `post_entry` 新增币种守卫：核对全部调用方（collect_payment / record_deposit / complete_refund / check_out_stay / night_audit.run）传入币种均等于 folio.currency（主账单开立即取 reservation.currency）；mock 回调路径直写 LedgerEntry 不经 post_entry，不受影响。
- `record_deposit / check_out_stay / _folio_for_update` 主账单限定：全库测试与非测试路径创建的 Folio 均为默认 MASTER，无行为回归。
- `_mask` 短值口径变更：全库唯一消费方为审计 `after_json`，无 API/测试断言依赖短值明文。

## 6. 最重要的三个发现

1. **WB-06（P1）退房账单可被非主账单劫持**：`check_out_stay` 按最新开立选取账单，分账单存在时房费、余额校验、关账全部落错对象，主账单未结清也可能完成退房——前台结算的资金正确性依赖一个未写出的隐含前提（所有账单都是主账单）。
2. **WB-05a（P1）押金上限可被脏读绕过**：累计押金上限校验信任调用方传入的内存快照且无行锁，并发/陈旧实例下可超上限多收押金并丢失并发写入；修复改为行锁下重读。
3. **WB-01（P2，4 处）金额量化崩溃**：所有金额入口对超出 Decimal 上下文精度的值（如 `1e30`）在 `quantize` 处抛未捕获 `InvalidOperation`，统一 500；已全部转为面向调用方的业务校验错误。
