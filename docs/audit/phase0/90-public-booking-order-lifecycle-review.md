# 90 · 公开预订订单生命周期审查报告（SEC-01~06 + 十一场景验收矩阵）

> 【新增 2026-11-07】针对 `635d9dd..b737489`（公开预订页令牌展示 + 续付面板）的外部评审意见，
> 逐条转化为代码修复、自动化测试与本文档验收标准。评审结论"方向正确、不回退"已采纳；
> 本报告为整改后的完整代码审查记录。

## 一、六项安全要求逐条核验

| # | 要求 | 结论 | 实现证据 |
|---|---|---|---|
| 1 | Token 高强度随机，不得由订单号/手机号/时间戳推导 | ✅ 达标（既有） | `apps/reservations/models.py:538` `secrets.token_urlsafe(32)`（256 bit 熵）；`test_s1` 断言令牌不含手机号、与订单号无关 |
| 2 | 数据库存安全摘要而非明文 | ✅ 达标（既有） | `apps/reservations/models.py:539` `hashlib.sha256(...).hexdigest()`，`token_hash` unique；`test_s1` 断言库中无明文 |
| 3 | 查询接口限流防爆破 | ✅ 达标（既有） | `apps/core/rate_limit.py:27` `public.reservation_query` IP 30/5min + booking+IP 10/5min（fail-closed）；`test_s10` 爆破 12 次触发 429 + 审计 |
| 4 | 支付接口服务端状态校验 + 幂等 | ✅ 达标（既有） | `apps/billing/services.py:36` `create_mock_payment_intent`：非 PENDING 拒绝（行锁）、重复点击返回同一 PENDING 支付单；`apps/billing/services_v2.py:412` 应收余额守卫；`test_s6/s7` 覆盖 |
| 5 | 取消订单服务端权限/状态校验 + 审计 | ✅ 达标（既有） | `apps/reservations/services.py` `cancel_pending_reservation` 仅 PENDING 可自助取消（EXPIRED 归档）；`apps/api/views.py` `reservation_cancel` 限流 + `guest.booking_cancelled` 审计；`test_s9` CHECKED_IN 取消被拒 409 |
| 6 | Token 不得写入 URL query/日志/埋点/错误追踪 | ⚠️ → ✅ **本次修复** | 评审发现续付表单曾用 `?token=`（会落服务器访问日志/浏览器历史）。已改为仅 `X-Booking-Token` 请求头：`apps/api/views.py:243`（query 回退删除）+ `openapi_schema.py` 契约同步；`test_sec01` 断言 query 传参一律 404。前端无埋点/错误追踪，审计事件不落 URL（`AuditEvent` 字段仅 action/entity/reason/request_id/ip/UA，`apps/core/models.py:62-89`） |

**遗留登记项**：员工邀请链接 `/invite/accept/?token=`（`apps/identity/invitation_services.py:66`）仍走 URL 传参——该令牌为一次性、短时效、激活需 POST 完成消费，属邮件激活链接行业惯例，与订单令牌威胁模型不同；如需整改另行立项（需业务决策，未擅动）。

## 二、验收场景矩阵 → 可执行结果

| 场景 | 预期 | 自动化测试（`apps/api/test_public_booking_lifecycle.py`） | 浏览器手工项 |
|---|---|---|---|
| S1 创建订单后复制 Token | 复制完整正确 | `test_s1_create_returns_one_time_token`（令牌返回+熵+不含手机号） | ✅ 复制按钮 + "已复制"反馈（浏览器回归通过） |
| S2 创建后立即刷新 | 可凭凭证找回 | `test_s2_and_s11_retrieve_after_refresh_without_cookies` | ✅ 浏览器回归通过 |
| S3 Token 输入错误 | 不返回订单信息 | `test_s3_s4_wrong_token_returns_404_without_info`（404 + 无订单字段 + 审计） | — |
| S4 编号正确 Token 错误 | 不返回订单信息 | 同上（空令牌/错令牌双分支） | — |
| S5 找回 PENDING | 可继续支付 | `test_s5_pending_retrieval_allows_payment`（detail 200 + intent 201） | ✅ 浏览器回归通过 |
| S6 找回 CONFIRMED | 不再显示支付入口 | `test_s6_confirmed_retrieval_closes_payment_entry`（intent 409）+ 前端状态门控 | ✅ 支付/取消按钮隐藏 |
| S7 连续点击支付 | 只产生一次有效支付 | `test_s7_double_click_creates_single_payment`（同一意图 + 单笔 Payment） | — |
| S8 支付响应丢失 | 可安全查询真实结果 | `test_s8_lost_payment_response_recovers_by_query` | — |
| S9 取消已入住订单 | 公共接口拒绝 | `test_s9_cancel_checked_in_rejected`（409 + 状态不变） | — |
| S10 多次错误 Token | 触发限流 | `test_s10_wrong_token_bruteforce_throttled`（429 + 审计） | — |
| S11 关浏览器重开 | 仍可凭凭证找回 | `test_s2_and_s11_...`（全新无 Cookie 客户端） | ✅ 浏览器回归通过 |
| SEC-01 | query 传令牌被拒 | `test_sec01_query_param_token_rejected` | — |

**测试基线**：SQLite 全量 887 项通过（0 fail / 0 error），含上述 11 项新测试。
（运行注记：`DJANGO_ENV=test`，`reconcile_test_users` 等命令测试的库门禁按环境判定。）

## 三、UI 交互整改（按评审建议落地）

1. **订单凭证独立区域（02 / BOOKING CONFIRMATION）**：状态徽章 + 预订编号 + 访问令牌行
2. **令牌一次性完整展示**：创建成功时展示完整令牌 + 「复制令牌」按钮（clipboard API，非安全上下文回退 execCommand）+ 复制成功/失败反馈
3. **脱敏回显**：找回订单后仅显示 `前4位••••••••后4位`，不再完整展示，复制按钮隐藏
4. **04 / ORDER RETRIEVAL 订单查询与管理**：面板定位从"仅续付"扩展为查询/查看/继续支付/按规则取消
5. **状态门控**（与项目实际状态机对齐，未新增状态）：PENDING → 支付+取消；CONFIRMED/CANCELLED/EXPIRED/CHECKED_IN/CHECKED_OUT/NO_SHOW → 仅查看（公共自助取消按现有服务端规则仅限 PENDING，未放宽）
6. **状态中文标签**：STATUS_LABELS 与 `Reservation.Status` 枚举一一对应
7. **异常分支恢复**：模拟支付无回调样例分支恢复按钮态（既有修复保留）

## 四、令牌丢失政策（明确声明）

- 令牌丢失**不存在手机号绕过**：`BookingAccessToken.validate` 仅接受 booking_no + plain_token（`apps/reservations/models.py:550`），手机号不参与任何公开接口鉴权（P0-08 基线不变）。
- 令牌丢失的订单自然归宿：PENDING 订单 15 分钟后库存 hold 过期释放，可重新预订；已支付订单由客人**线下联系酒店**，由员工按店内身份核验流程在前台工作台处理（前台操作走员工鉴权链，不走公共接口）。
- 公开预订 Token **不创建**任何账户：全链路无 Guest User/Django User/PMS 登录账户生成（员工-住客账户分离基线不变；`test_register_redirects_and_creates_nothing` 持续守护）。

## 五、变更清单（本次整改）

| 文件 | 变更 |
|---|---|
| `apps/api/views.py` | SEC-01：删除 query token 回退，仅 X-Booking-Token 头 |
| `apps/api/openapi_schema.py` | 契约同步（移除 query 参数描述，标注 SEC-01） |
| `docs/接口文档/openapi.json` | 快照再生成（102 paths） |
| `templates/public/booking.html` | 凭证区/复制按钮/脱敏/ORDER RETRIEVAL/状态门控/中文状态标签 |
| `apps/api/test_public_booking_lifecycle.py` | 新增 11 项验收测试 |
| `apps/api/test_openapi_schema.py` | 契约断言同步 SEC-01 |
