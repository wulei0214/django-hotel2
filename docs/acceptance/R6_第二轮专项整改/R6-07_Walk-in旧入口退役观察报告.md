> 文档编号：R6-07 ｜ 类型：DECOMMISSION-OBSERVATION ｜ 状态：CURRENT ｜ 版本域：PMS2.0 当前版本 ｜ 业务模块：R2 Walk-in 旧入口退役观察

# R6-07 Walk-in 旧入口退役观察报告

| 元信息 | 值 |
| --- | --- |
| 任务书依据 | §四 TASK-06（R2 Walk-in 旧入口退役观察） |
| 权威证据源 | `docs/PMS2.0_当前版本/04_开发实施记录/Walk-in双端点统一与观察统计R2实施记录_2026-11-19.md` + `apps/api/test_walk_in_api.py` + `apps/api/front_desk.py:1785-1811` |
| 退役提交 | 观察埋点 `R2-03` → 410 退役 `e56164d`（DEC-WI-01 收敛收口）+ `8ebf918`（csrf_exempt，旧客户端裸 POST 也收 410） |
| 决策基线 | DEC-WI-01（散客直入双端点收敛） |
| 独立验收 | G14 已于 2026-11-20 确认通过 |

---

## 一、入口策略与状态

| 入口 | 状态 | 行为 |
| --- | --- | --- |
| `POST /api/v1/front-desk/walk-ins` | 唯一正式业务入口 | 标准 A1 契约，全业务写入 |
| `POST /api/v1/front-desk/walk-in` | **已退役，恒返 410 Gone** | 不区分认证/权限/入参，一律 410 + `error.code=GONE` + 指向标准入口；不读请求体、不做业务写入、不记观测审计 |

代码位置：`apps/api/front_desk.py:1785-1811`（walk_in_create 恒 410）；csrf_exempt 保证裸 POST 也返回明确 410 而非 403（`8ebf918`）。

## 二、410 端点零业务写入实证

`apps/api/test_walk_in_api.py` 将原 9 项兼容契约测试逐一转化为 410 退役行为测试，统一断言 `_assert_retired()`（410 + GONE + 指向标准入口 + 零写入）：

| 测试 | 原场景 | 退役后断言 |
| --- | --- | --- |
| test_walk_in_retired_valid_payload_returns_410_without_writes | 合法入参 201 | 410，Reservation/Guest/Stay/Payment/Folio 计数不变 |
| test_walk_in_retired_idempotent_key_replay_returns_410 | 同幂等键重放 | 两次均 410，不建档、不写幂等记录 |
| test_walk_in_retired_missing_*/no_vacant_room_* | 缺参/无净房 400 | 一律 410，无孤儿记录 |
| test_walk_in_retired_anonymous_returns_410 | 匿名 401 | 匿名与合法会话均 410 |
| test_walk_in_retired_records_no_observation_audit | 兼容期观测审计 | 观察期随退役结束，410 路径不新写 `frontdesk.legacy_walk_in_called` |

结论：旧入口不创建 Guest、Reservation、Stay、Payment、Folio 任何业务记录（任务书 TASK-06 第 3 条）。

## 三、观察统计与来源核查（如实区分）

| 项 | 结论 | 依据 |
| --- | --- | --- |
| 前端/模板/内部代码调用面 | **零调用** | R2-01 全仓 grep 复证：static/templates/hotelapp 无命中；apps 内仅路由注册、docstring、契约声明三处非调用引用 |
| 兼容期观察埋点 | 已实现并测试 | `frontdesk.legacy_walk_in_called` 审计（entity_type=FrontDeskEndpoint、entity_id=front-desk/walk-in，含 request_id/ip/user_agent），`test_walk_in_legacy_call_records_observation_audit` 双向锁定"观测不写业务" |
| 观察触发条件 | 连续 ≥1 营业日无观测事件 | R2 实施记录 §2.3；因前端/内部零调用，退役条件满足 |
| 退役后残留调用统计 | **证据边界声明** | 退役后端点恒 410 且不再记观测审计（观察期随退役结束）；本证据目录为开发/本地验收环境，**无生产环境 410 命中样本**。残留调用统计由部署侧 Web 访问日志承接（兼容期已验证 request_id/ip/user_agent 采集能力）——不得据此宣称"生产残留调用为零" |

## 四、迁移方案与责任人

- 迁移方案：所有调用方改用 `POST /api/v1/front-desk/walk-ins`（DEC-WI-01），410 响应体已内置指引文案。
- 责任人：DEC-WI-01 收敛记录指定前台接待模块责任人；因全仓零调用，无待迁移的存量客户端。
- 回滚：git revert 单提交即可恢复兼容端点；观测事件为追加式审计事实，无需清理。

## 五、结论与边界

旧入口退役治理在**代码与契约层完整闭环**：唯一正式入口确立、旧入口恒 410、零业务写入、9 项退役测试锁定、兼容期观察能力已验证。按任务书纪律，**不以证据不足的"生产残留调用为零"作为结论**——生产环境 410 命中次数/客户端/路径的统计需由部署侧访问日志在运行期采集，本报告如实登记该边界。独立验收 G14 确认通过。
