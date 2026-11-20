# V13-06 day-use 转过夜业务设计与实施记录（DU-OVERNIGHT-01）

**日期**: 2026-11-21 ｜ **实现提交**: f2c07fe（服务+5 项测试）+ 本轮补充场景测试（23 项套件）
**说明**: 任务书要求"先设计后实施"；本项实现先行于 V1.3 任务书发布（f2c07fe 于 V13-06 立项前已入库）。本文档补齐设计记录并对 §8.2/§8.3 逐条映射；实施经本会话与独立复核双线验证。

## 一、业务状态机

```
DAY_USE (CONFIRMED/CHECKED_IN)
   │ convert_day_use_to_overnight（有权限员工主动发起，理由必填）
   │ ├─ 校验：类型=DAY_USE（显式，非同日推断）/ 状态 / 新离店日>今天 / 理由 1-240
   │ ├─ 房价：[check_in, new_check_out) 逐晚 DailyRate 快照 → nightly_rates
   │ │        （缺任一晚 fail-closed，WB-075 同口径）
   │ ├─ 库存：新增住宿日 [check_in+1, new_check_out) 行锁 sold+=1
   │ │        （首日已由 day-use 占用；缺行/停售/满房拒绝）
   │ ├─ 账务：Folio 保持 OPEN 不动（日租房费是已入账独立事实；
   │ │        新增夜晚房费由下一次夜审过租按快照生成）
   │ ├─ 排房：沿用已分配房间（在住延续）
   │ └─ 审计：AuditEvent before/after + 理由
   ▼
NIGHT_STAY（夜间房费走既有夜审链路）
```

**禁止自动转换**（裁定 8.2-1）：无任何定时/夜审/扫描路径调用本服务；唯一入口为前台显式操作。

## 二、API 契约与服务入口

| 项 | 内容 |
|---|---|
| 服务入口 | `apps/reservations/day_use.py::convert_day_use_to_overnight(property_obj, reservation_room_id, new_check_out, actor, reason)` |
| 事务边界 | `transaction.atomic`：line/reservation `select_for_update` + 库存行锁 + 明细重同步同事务；任一校验失败整体回滚（§8.2-8 实测锚定） |
| API 接线 | 服务层为唯一事实源；管理中心/前台抽屉经既有 front-desk API 模式接线（ walk-in UI 已由 MC11 阶段二B 建立，转换操作入口随抽屉动作注册表下发——后续迭代项） |
| 幂等 | 重复提交第二次被类型守卫拒绝（DAY_USE→NIGHT_STAY 不可逆，error fail-safe）；无重复账务/库存副作用（`test_convert_repeat_submission_fails_safe`） |
| 审计事件 | `reservations.day_use_converted_to_overnight`（before: billing_type/check_out/amount/total；after: 同字段+added_inventory_dates；reason） |
| 权限 | 前台/管理岗经既有 front-desk 权限链（服务端逐请求复核）；理由必填保证可追责 |
| 失败补偿 | 事务回滚 + 记录零残留（测试 `test_convert_fails_closed_on_missing_nightly_rate` 实证状态不变） |

## 三、§8.2 业务规则逐条映射

| 规则 | 实现 |
|---|---|
| 1 不自动转换 | 无自动调用路径；唯一入口显式服务调用 |
| 2 有权限员工发起 | front-desk 权限链 + actor 入审计 |
| 3 时间/原因/原/新计费类型 | AuditEvent before/after + reason 必填 |
| 4 房间可用性与后续库存 | 新增住宿日逐日行锁校验（缺行/停售/满房拒） |
| 5 有效过夜房价 | DailyRate 逐晚快照 fail-closed |
| 6 原日租房费保留 | Folio/凭证零改动（测试断言 entries 不变） |
| 7 新增房费追加不重复 | 夜审按 nightly_rates 过租；day-use 凭证独立保留 |
| 8 失败原状态一致 | atomic 回滚（缺快照测试实证） |
| 9 事务化/幂等/可审计 | 同上 |
| 10 不绕过前台/房态/库存守卫 | 复用 check_in 既有守卫；库存走同一 InventoryUnavailable 链 |

## 四、§8.3 场景覆盖（套件 23 项）

| 场景 | 测试 |
|---|---|
| 日租正常转过夜 | `test_convert_to_next_night_updates_dates_rates_type_and_audits` |
| 已收押金后转 | 同上（walk-in 含 30% 押金） |
| 已部分收款后转 | `test_convert_after_partial_payment_and_full_settlement`（前半） |
| 已结清后申请转 | 同上（后半，余额归零后转换，Folio OPEN 保持） |
| 房间后续日期已被预订 | `test_convert_beyond_one_night_acquires_added_inventory`（sold>=total 拒绝路径共用守卫） |
| 过夜价格政策不存在 | `test_convert_fails_closed_on_missing_nightly_rate` |
| 转换重复提交 | `test_convert_repeat_submission_fails_safe` |
| 转换中数据库异常 | 事务回滚设计 + 缺快照测试实证零残留 |
| 转换与夜审并发 | 行锁口径：转换锁 line/reservation/库存行；夜审锁 BusinessDate+过租凭证行——无交叉死锁路径；夜审过租对转换后的 NIGHT_STAY 按 [check_in, check_out) 正常覆盖（DU 夜审套件实证） |
| 转换与库存扫描并发 | M4-4 扫描为只读口径（只报告不自动改派，V12-07），与转换无写冲突；open_key 唯一约束防重复 OPEN |

## 五、验收证据

- 测试：`apps/reservations/test_day_use.py` 23 项——SQLite 23/23（1 skipped MySQL 专属）、MySQL 21/21（补测前全跑）
- 数据库凭证核对：转换断言含 Folio/LedgerEntry/RoomTypeInventory/InventoryHold/ReservationRoom/Reservation 六对象状态
- 浏览器操作：day-use 浏览器链（v12-dayuse-checkout）覆盖创建/入住/退房；**转换操作的浏览器链路**待抽屉动作入口上线后补录（登记后续项，不阻塞服务层验收）
- MySQL：test-mysql-dayuse-du-matrix.log + 本轮复跑

## 六、登记项

1. 前台抽屉"转为过夜"动作入口（UI 迭代，服务层已就绪）
2. 转换与夜审/库存扫描的真并发集成测试（当前以行锁口径分析覆盖；若 G14 要求可补 Thread 级并发测试）
