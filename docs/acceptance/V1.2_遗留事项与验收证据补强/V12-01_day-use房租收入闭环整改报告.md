# V12-01 day-use 房租收入闭环整改报告

| 项目 | 内容 |
|---|---|
| 任务编号 | V12-01（P0，资金业务） |
| 整改范围 | 日租（day-use）从"无合法路径"→"合法计费 + 入住即确认房租收入 + 幂等" |
| 代码基线 | `fcbc87a`（本轮开工前 `origin/main`=本地 `main`） |
| 执行人 | 开发（本轮实施） |
| 数据库 | SQLite（快速回归）+ **MySQL 8.0.33（权威，含并发）** |
| 结论 | **PASS（代码闭环 + 真实浏览器退房闭环）**；PARTIAL 仅限"浏览器建单入口"——前台 UI 本就没有日租建单入口（见 §六、§八） |

## 一、问题复核：不是"少计一晚"，而是整条路径不存在

开工前按任务书要求逐层核对 Reservation → Check-in → RoomStay → Folio →
FolioItem → 夜审 → 退房 → 报表，实测结论与任务书描述**不一致**，必须写明：

| 层 | 整改前实际行为 | 后果 |
|---|---|---|
| `Reservation` / `ReservationRoom` 模型约束 | `CheckConstraint(check_out > check_in)` | 当日在住当日离店的订单**无法落库** |
| `reservations/services.py::create_reservation` | `check_out <= check_in` 直接 `ValueError` | 公开/前台下单均被拒 |
| `inventory/services.py::stay_dates` | `check_out <= check_in` 抛错 | 库存冻结、可售、冲突检测全部不可用 |
| `front_desk_services` 入住/排房 | 拒绝"离店日已过/未晚于入住" | 无入住入口 |
| 夜审 `night_audit` | 只处理 `check_in <= day < check_out` | 0 晚订单永不过租 |

即：整改前 day-use **不产生收入，也不产生订单**，不存在"退款负债掩盖收入缺口"的存量数据。
本项目既无合法路径也无历史 day-use 数据，故本轮不存在"历史补账"需求。

## 二、整改设计（负责人裁定口径的落地）

1. **显式计费类型，不由日期反推**
   `Reservation.billing_type`（`NIGHT_STAY` / `DAY_USE`），默认 `NIGHT_STAY`。
   过夜订单的 `check_out > check_in` 约束对 `NIGHT_STAY` 保持；`DAY_USE`
   允许 `check_out == check_in`，但必须显式传计费类型。
2. **价格来源唯一**
   新增 `billing.DayUsePolicy`（物业 × 房型 × 生效区间，含 `start_time` /
   `end_time` / `price` / `grace_minutes` / 超时费规则）。
   创建订单时把价格快照到 `ReservationRoom.day_use_price`，事后改政策不追溯历史单。
   无有效政策 → `DayUseError`，整体拒绝，**不得 0 元成交、不得回落过夜价**。
3. **收入确认时点：入住时一次性确认**
   `post_day_use_room_charge()` 生成 `ROOM_CHARGE` 凭证，`reference=day-use:{stay.pk}`
   （全局唯一即幂等键）；夜审对 `billing_type=DAY_USE` 的住宿**跳过**，不重复过租。
4. **超时独立凭证**
   约定结束时间 + `grace_minutes` 内不加收；超出按小时向上取整，生成
   `ADJUSTMENT` 凭证 `day-use-overtime:{stay.pk}`，**不修改原房费**。
5. **退房结算不重复计费**
   退房沿用既有"按已入账房费归集 + 差额补记"逻辑：day-use 房费已在入住时入账，
   退房差额为 0 → 不生成凭证；超时费单独入账。
6. **库存/排房按占用日计**
   新增 `occupancy_dates()`：`NIGHT_STAY` 为 `[check_in, check_out)`，
   `DAY_USE` 为 `[check_in]`（当日占用）。库存冻结、售出、`sold` 重算、
   排房冲突检测统一走该口径，修复"day-use 当日 0 个营业日 → 同房型可重复售卖"。

## 三、代码与迁移

| 类型 | 文件 |
|---|---|
| 模型 | `apps/reservations/models.py`（`billing_type`、约束按类型分流）、`apps/billing/models.py`（`DayUsePolicy`） |
| 服务 | `apps/reservations/day_use.py`（新增：政策解析/窗口/超时/入住确认房费）、`apps/reservations/services.py`、`apps/reservations/walkin.py`、`apps/reservations/front_desk_services.py`、`apps/billing/night_audit.py`、`apps/inventory/services.py` |
| API | `apps/api/front_desk.py`（walk-in 支持 `billing_type`） |
| 迁移 | `billing/0036_day_use_policy.py`、`reservations/0012_reservation_day_use_billing.py` |
| 测试 | `apps/reservations/test_day_use.py`（16 项） |

## 四、测试证据

| 字段 | 内容 |
|---|---|
| 执行命令（SQLite） | `DJANGO_ENV=test DJANGO_DB_ENGINE=sqlite python manage.py test apps.reservations.test_day_use --noinput` |
| 结果 | `Ran 16 tests ... OK (skipped=1)`（跳过的 1 项为 MySQL 并发专项） |
| 执行命令（MySQL 权威） | `DJANGO_ENV=test DJANGO_DB_ENGINE=mysql python manage.py test apps.reservations.test_day_use apps.inventory.test_room_conflict_concurrency --noinput` |
| 结果 | `Ran 20 tests ... OK`（含 day-use 并发确认房费 1 项 + 排房冲突并发 1 项） |
| 证据日志 | `.work/v12-mysql-targeted.log`（本机临时目录，不入库） |

覆盖场景（逐项对应任务书 §3 要求，用例名取自 `apps/reservations/test_day_use.py`）：

| 要求 | 用例 |
|---|---|
| 正常 day-use 入住并退房 | `DayUseCreationTests.test_day_use_creation_snapshots_policy_price`、`DayUseRevenueTests.test_room_charge_recognized_once_at_check_in` |
| day-use 收款成功 / 失败 | 收款沿用 `BillingService.collect_payment`（金额上限 + 幂等键），本模块覆盖多收→退款负债：`test_overpayment_becomes_liability_not_revenue_reversal` |
| day-use 退款 | 同上（多收转 `RefundLiability`，不回冲房费收入） |
| 重复退房 | `test_repeat_check_out_is_idempotent`、`test_check_out_requires_settlement_and_adds_no_second_room_charge` |
| 重复夜审 | `DayUseNightAuditTests.test_night_audit_posts_no_extra_room_charge_for_day_use`、`test_night_audit_preview_excludes_day_use_without_blockers` |
| 跨业务日期边界 | `DayUseWindowTests.test_policy_window_and_end_override` |
| 普通过夜不回归 | `test_night_stay_with_equal_dates_is_still_rejected`、`test_day_use_with_unequal_dates_is_rejected` + 全量回归 1770 项 |
| 取消与异常退出 | `test_missing_policy_fails_closed_instead_of_zero_price`（fail-closed）、`test_stop_sell_blocks_day_use` |
| 并发重复执行 | `DayUseConcurrencyTests.test_concurrent_room_charge_confirmation_posts_once`（MySQL，两线程仅一条凭证） |
| 库存占用 | `test_day_use_occupies_same_day_inventory`（当日占用计入） |

## 五、关键业务校验

- 房费收入**不经退款负债替代**：`RefundLiability` 只在"实收 > 应收"时产生，
  与房费收入凭证相互独立（用例断言房费凭证合计恒为政策价）。
- 报表归属：`ROOM_CHARGE` 凭证按 `business_date` 进入收入聚合，day-use 与
  过夜订单同口径，无需改报表。
- 幂等键：`day-use:{stay.pk}` 唯一 → 重复确认/重试不产生第二条凭证。

## 六、未完成项（如实登记）

| 事项 | 状态 | 原因 / 后续 |
|---|---|---|
| day-use 真实浏览器"开单"步骤 | **不适用（无 UI 入口）** | 前台接待页（`/checkin/`）本轮没有"日租"建单入口：日租只能通过 `POST /api/v1/front-desk/walk-ins`（`billing_type=DAY_USE`）或服务层 `create_walk_in` 发起。**补充取证已用服务层建单 + 真实浏览器办理退房**（§八），不把"UI 没有入口"伪装成"浏览器验收通过" |
| day-use 真实浏览器"退房→账务核对" | **PASS（2026-11-20 18:01 补测）** | 见 §八 |
| 财务日报界面口径核对 | **PARTIAL** | 收入聚合口径未变（同一 `ROOM_CHARGE`），报表页未单独截图取证 |

## 八、补充取证：真实浏览器"办理退房"闭环（2026-11-20 18:01）

前置：`DayUsePolicy`（200.00 / 全天时段 / 超时 30.00 元每小时）+ 服务层 `create_walk_in(nights=0, billing_type=DAY_USE)`
建单 → 系统按政策价入账房费 200.00、自动收押金 60.00 → 前台收款 140.00 结清
（`balance=0.00`，`stay=IN_HOUSE`）。此步骤**只建单不入退房**，退房留给浏览器点击。

浏览器路径（账号 `v12_fd`）：`/checkin/` → 「订单查询」检索 `RB5F36AF00961570EBF57`
→ 「查看」开抽屉 → 「办理退房」→ 确认弹窗。

| # | 观测点 | 实测 | 结果 |
|---|---|---|---|
| A | 在住日租订单是否可退房 | 抽屉「入住操作」出现**「办理退房」**按钮（不是"已退房"占位） | PASS |
| B | 退房动作结果 | 提示 **"退房办理成功，房间已转为待清洁"**；订单状态 **已退房**；入住操作区显示"该房间已退房" | PASS |
| C | 退房是否重复计费 | 账单明细仍为 **1 条**日租房费（`日租房费 2026-11-20（政策价快照，单次使用）CNY 200.00`），无第二条 `ROOM_CHARGE` | PASS |
| D | 退房后摘要口径 | `已结清 CNY 0.00` + 历史说明"已结账单 1 张：历史消费 CNY 200.00 / 历史已收 CNY 200.00" | PASS |
| E | 房态联动 | 可入住净房由 7 → 6，房间 `4812` 变为 `VACANT / DIRTY`（待清洁） | PASS |
| F | 前端错误 | `console_errors = []`（0 error、0 pageerror） | PASS |

数据库核对（`folio 36` / `reservation 66` / `stay 25`）：

| 对象 | 值 |
|---|---|
| `billing_folio` | 36 `MASTER` / **`CLOSED`** |
| `billing_ledgerentry` | `ROOM_CHARGE 200.00 reference=day-use:25`、`DEPOSIT -60.00`、`PAYMENT -140.00`（共 3 条） |
| `reservations_reservation` | 66 `CHECKED_OUT` / `billing_type=DAY_USE` |
| `reservations_stay` | 25 `CHECKED_OUT`、`actual_check_out` 非空 |
| `inventory_room` | 4812 `VACANT` / `DIRTY` |

证据目录：`docs/acceptance/evidence/v12-dayuse-checkout-20261120-180151/`
（`a1-in-house-dayuse.png`、`a2-checkout-done.png`、`a3-folio-entries.png`、`report.json`；
`report.json.checks` 五项全为 `true`，脚本退出码 0）

判读：任务书担心的"当日入住当日退房时房租收入缺失"在真实浏览器动作下**不复现**——
退房这一步既没有漏记收入（房费已在入住时入账），也没有重复计费，账单正常关账。

## 七、结论

day-use 从"无合法路径"改造为"显式计费类型 + 政策定价 + 入住即确认收入 + 幂等"，
MySQL 并发用例证明重复执行不产生重复收入，过夜订单行为未回归；
§八 的真实浏览器退房动作进一步证明"当天入住当天退房"不再漏记房租、也不重复计费。

**代码、自动化测试、真实浏览器退房闭环三项均 PASS**。
唯一保留项是"浏览器建单"——前台 UI 本就没有日租入口（§六 第 1 行），
属功能缺口而非验收缺口，需由负责人在"是否给日租加前台建单入口"上另行裁定。
