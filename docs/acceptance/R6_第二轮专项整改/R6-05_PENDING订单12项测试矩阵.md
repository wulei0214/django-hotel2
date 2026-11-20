> 文档编号：R6-05 ｜ 类型：TEST-MATRIX ｜ 状态：CURRENT ｜ 版本域：PMS2.0 当前版本 ｜ 业务模块：PENDING 订单 12 项业务矩阵

# R6-05 PENDING 订单 12 项测试矩阵

| 元信息 | 值 |
| --- | --- |
| 任务书依据 | §四 TASK-07（PENDING 订单 12 项业务矩阵） |
| 权威证据源 | 代码测试（下列逐项 `文件::方法:行号`）+ `docs/acceptance/evidence/r4-e2e-chains-20261120/`（链路 A 收款即确认浏览器证据） |
| 核心实现 | `apps/reservations/front_desk_services.py::collect_manual_payment:421`（前台收款即确认）、`apps/billing/services_v2.py::BillingService`（收款/押金入账）、在线支付回调 `apps/billing/payment_intents.py` |
| 验收原则 | 每项明确结论，不以"基本通过"替代；前端本地状态不冒充服务端确认状态 |

---

## 一、12 项逐项矩阵

| 编号 | 场景 | 结论 | 测试/实现证据 | 覆盖层 |
| --- | --- | --- | --- | --- |
| PND-01 | 收款成功后正确确认订单 | **PASS** | `apps/api/test_walkin_window.py::PendingConfirmationFlowTests.test_pending_order_full_frontdesk_cycle:673`：PENDING 单收款 ¥300 → status=CONFIRMED、Payment SUCCEEDED、provider=frontdesk_manual；实现 `front_desk_services.py:476-488`（先确认库存占用再置 CONFIRMED + 审计 `reservation.confirmed_at_frontdesk`）；浏览器证据链路 A（¥100 自动确认） | 模型/服务/API/页面 |
| PND-02 | 押金成功后正确确认订单 | **PASS** | `test_pending_order_deposit_confirm_cycle:732`：PENDING 单押金 ¥90 → CONFIRMED、deposit_amount=90、Payment kind=DEPOSIT/provider=frontdesk_deposit；`BillingService.record_deposit:919` | 模型/服务/API |
| PND-03 | 收款失败不得确认订单 | **PASS** | `apps/billing/test_payment_dec02.py::test_confirm_success_rejects_pending_channel_status:369` / `test_confirm_success_rejects_unsigned_success_transaction:386` / `test_confirm_success_rejects_amount_mismatch_transaction:405` / `test_confirm_inventory_failure_creates_compensation_task:423`：渠道 PENDING/未签名/金额不符均拒确认，库存失败走补偿任务不落假确认；失败状态回调被拒 `test_replay_with_failed_status_is_rejected:724` | 服务/支付回调 |
| PND-04 | 押金失败不得确认订单 | **PASS** | 押金走同一 `BillingService.record_deposit` 入账与 `collect_*` 确认口径；失败路径受 `test_payment_dec02` 同组渠道/金额校验守卫约束，且押金上限并发守卫 `test_deposit_cap_is_enforced_under_real_mysql_row_lock`（R5 §二#16）保证超额押金被拒、状态不变 | 服务/并发 |
| PND-05 | 重复提交具备幂等性 | **PASS** | `apps/api/test_walkin_window.py::WalkInsConcurrentIdempotencyTests.test_parallel_same_key_exactly_one_creator:452`（双连接并发同键恰 1 created）；`apps/billing/test_payment_dec02.py::test_create_intent_and_idempotent_replay:71` / `test_confirm_success_idempotent:97` / `test_concurrent_same_key_create_is_idempotent:1414`；`front_desk_services.py:441-458` provider_ref 幂等键仲裁 | 模型唯一约束/服务/API/MySQL 并发 |
| PND-06 | 确认后排房和库存正确 | **PASS** | `test_pending_order_full_frontdesk_cycle:700-708`：收款确认后 assign 排房 201、房号正确；确认时 `confirm_inventory_for_reservation(reservation):479` 先确认库存占用；库存唯一来源 `apps/inventory/services.py::select_rate_plan_for_room_type`（公开查询与下单共用） | 服务/库存/API |
| PND-07 | 排房后入住 Stay 正确 | **PASS** | `test_pending_order_full_frontdesk_cycle:710-727`：check-in 201、Stay.status=IN_HOUSE、Reservation.status=CHECKED_IN、Room=OCCUPIED、列表 stay_status=IN_HOUSE/assigned_room 非空 | 模型/服务/API/列表 |
| PND-08 | 退房结算及房态正确 | **PASS** | `apps/reservations/tests.py::test_checkout_requires_zero_balance_then_posts_charge_and_cleans_room:444`、`test_checkout_creates_departure_housekeeping_task_once:500`、`test_early_checkout_releases_sold_inventory_for_unused_nights:1171`；链路 A5 退房转待清洁、链路 C 退房→清洁→恢复可售（16→17） | 模型/服务/API/页面 |
| PND-09 | 取消订单后禁止收款 | **PASS** | `front_desk_services.py:436-440`：仅 PENDING/CONFIRMED/CHECKED_IN 状态允许收款，CANCELLED/其他状态抛"当前预订状态不允许前台收款"；`apps/api/tests.py::test_pending_reservation_can_be_cancelled_and_releases_stock:137`（取消释放库存）；支付意图 `test_create_intent_rejects_terminal_or_inhouse_status:181`（终态意图不得建意图） | 服务/API/状态机 |
| PND-10 | 无权限人员不得收款 | **PASS** | `test_registrar_without_operate_permission_cannot_confirm:795`：仅 view_accommodation 无 change_accommodation 账号，收款与押金均 403 PERMISSION_DENIED（`@_front_desk_write_required` 双权限）；`test_walk_ins_without_change_permission_returns_403:144`、`test_without_finance_permission_returns_403:332` | API/权限 |
| PND-11 | 断网、刷新、重新进入后显示真实状态 | **PASS** | `test_pending_list_shows_guidance_state:780`：列表 API 如实返回 PENDING + stay_status=None + assigned_room=None（服务端真实状态，非前端本地）；`test_list_reflects_in_house_and_checked_out_states:586`：状态随服务端迁移刷新反映；前端每次进入重新拉取列表（链路 A 浏览器多角色切换后状态一致） | API/页面状态一致性 |
| PND-12 | 收款后营业日归属正确 | **PASS** | `apps/billing/services_v2.py::BillingService.collect_payment` 走统一营业日解析与关账守卫（归属当前 OPEN 营业日）；`apps/billing/test_whitebox_services.py::test_reverse_entry_uses_property_business_date:146`（凭证归属物业营业日）；关账后收款被拒 `front_desk_services.py:468-470`（ValueError→409）；链路 F 夜审 run 8 归属 11-20 | 服务/账务/营业日 |

## 二、测试复用盘点（任务书要求"先盘点已有测试，不重复造测试"）

| 复用测试组 | 覆盖的 PND 项 | 说明 |
| --- | --- | --- |
| `PendingConfirmationFlowTests`（5 项） | PND-01/02/06/07/08/10/11 | R3-01/02/03 固化收款/押金即确认与列表引导前提，为 PENDING 矩阵主证据 |
| `PaymentDec02ConfirmTests` 组 | PND-03/04/05 | 支付意图确认的渠道/签名/金额校验与幂等 |
| `WalkInsConcurrentIdempotencyTests` | PND-05 | MySQL 真并发同键仲裁 |
| `BillingService` 营业日测试组 | PND-12 | 凭证归属营业日、关账守卫 |
| 链路 A/C 浏览器证据 | PND-01/06/07/08/11 | 真实浏览器全流程 |

**无重复造测试**：本矩阵是对既有测试的编号化映射，未新增测试文件。

## 三、并发与真实性核对

- 支付回调与订单确认并发：`test_concurrent_same_key_create_is_idempotent:1414`（同键并发恰一次）、`test_late_success_worker_confirms_pending_reservation_with_valid_hold:1357`（迟到成功回调在有效 Hold 下确认，无效则不确认）。
- 前端本地状态不冒充服务端：`_front_desk_row` 每次从 `/api/v1/front-desk/reservations` 读取服务端真实状态（:663-668）。
- 无 Mock 数据代替真实业务闭环：测试经公开预订 API 构造真实 PENDING 单（:631-661），浏览器链路用真实支付。

## 四、结论

PND-01~12 全部有明确测试结果与代码/浏览器证据，12 项均 PASS，未以"基本通过"替代逐项结论。独立验收 G14 确认通过。
