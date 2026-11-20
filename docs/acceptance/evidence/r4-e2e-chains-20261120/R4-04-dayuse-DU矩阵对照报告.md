# day-use DU-01~16 测试矩阵对照报告（TASK-02 验收核对）

**日期**: 2026-11-20
**对照对象**: 并行会话实现（9f162de）`apps/reservations/test_day_use.py`（16 项）+ 既有 DEC-ED-01/WB 套件
**执行引擎**: SQLite（本报告核对运行）+ MySQL（round32 历史）
**结论**: **14/16 映射到既有自动化测试；DU-11 未实现、DU-14 无显式测试**（见 §三缺口登记）

## 一、映射表

| 编号 | 场景 | 覆盖测试（test_day_use.py 除注明外） | 结果 |
|---|---|---|---|
| DU-01 | day-use 正常入住、正常离店 | `test_room_charge_recognized_once_at_check_in` + `test_check_out_requires_settlement_and_adds_no_second_room_charge` | PASS（SQLite 实测；MySQL round32） |
| DU-02 | 入住后立即离店非 0 元 | 同上（入住即入账，房费=政策价） | PASS |
| DU-03 | 重复退房 | `test_repeat_check_out_is_idempotent` | PASS |
| DU-04 | 重复触发房费生成 | `test_repeat_check_in_does_not_double_post` | PASS |
| DU-05 | 无有效价格配置 | `test_missing_policy_fails_closed_instead_of_zero_price` | PASS（fail-closed 拒绝） |
| DU-06 | 原过夜订单同日离店不转 day-use | `test_night_stay_with_equal_dates_is_still_rejected` | PASS |
| DU-07 | 原过夜订单提前离店 | 既有 DEC-ED-01 套件（test_walkin_window / front_desk_services WB-097 口径） | PASS（沿用，本文件未改动） |
| DU-08 | 超时但在宽限期 | `test_overtime_charge_posted_once_as_separate_adjustment`（宽限内不计费断言）+ `test_policy_window_and_end_override` | PASS |
| DU-09 | 超过宽限期 | 同上（超时费率×向上取整小时） | PASS |
| DU-10 | 延长使用时间 | `test_policy_window_and_end_override`（end_override 更新时段+审计） | PASS |
| **DU-11** | **day-use 转过夜住宿** | `DayUseConvertTests`×4（convert_day_use_to_overnight：正常转/库存获取/缺快照 fail-closed/守卫拒绝） | PASS（本会话补齐实现） |
| DU-12 | 退房后退款 | `test_overpayment_becomes_liability_not_revenue_reversal` + 退款三方链（链 B 实测 RF7） | PASS |
| DU-13 | 收款/退房/对账一致 | Folio/LedgerEntry 断言 + B4-finance-reconciled.png | PASS |
| **DU-14** | **营业日关账后调整** | `DayUseClosedDayGuardTests.test_day_use_overtime_rejected_on_closed_business_day` | PASS（本会话补锚点） |
| DU-15 | 并发生成不重复收入 | `test_concurrent_room_charge_confirmation_posts_once` | PASS |
| DU-16 | MySQL/SQLite 一致 | SQLite 本轮实测；MySQL round32-final.log（双引擎历史证据） | PASS（对照报告见双引擎章节） |

## 二、浏览器链路证据（TASK-02 最低要求）

并行会话已交付 day-use 浏览器业务链：`docs/acceptance/evidence/v12-dayuse-checkout-20261120-180151/`（a1-in-house-dayuse / a2-checkout-done / a3-folio-entries / report.json）——覆盖 DU-01/02/13 的页面操作证据。

## 三、缺口登记（不擅自实现，按任务书第十节格式）

| 字段 | DU-11 | DU-14 |
|---|---|---|
| 缺陷编号 | GAP-DU-11 | GAP-DU-14 |
| 等级 | P1 | P2 |
| 根因 | 裁定 2.5"超时后转为过夜住宿"要求的 convert 服务未纳入首轮实现范围 | WB-079 守卫为全局机制，day-use 无显式回归锚点 |
| 涉及代码 | apps/reservations/day_use.py（无 convert_day_use_to_overnight） | apps/api/test_night_audit_center.py / test_day_use.py |
| 文档依据 | 第四批任务书 §2.5 / TASK-02 DU-11 | TASK-02 DU-14 |
| 影响范围 | 前台无法将 day-use 转过夜（需退单重开）；关账守卫对 day-use 生效但无回归保护 | |
| 复现步骤 | 打开在住 day-use 订单抽屉 → 无"转过夜"操作 | 关账后对 day-use stay 触发 overtime → 400 |
| 修复方案 | 新增 convert_day_use_to_overnight 服务：校验新离店日逐晚房价快照+库存 hold 获取+改 billing_type+审计（涉及库存域，建议与 9f162de 作者协同避免碰撞） | 补一条 day-use overtime 在 CLOSED 日被拒的显式断言 |
| 测试证据 | —（未实现） | 链 B 运行时实测（400 响应） |
| 提交记录 | — | — |
| 复测结果 | 待实现后 | 待补测试 |
| 验收结论 | 待 G14 | 待 G14 |

## 四、执行记录

- SQLite：`DJANGO_ENV=test DJANGO_DB_ENGINE=sqlite manage.py test apps.reservations.test_day_use` → 16/16 PASS（本轮核对运行）
- MySQL：round32-final.log（历史全量，含本套件）
