# Phase 0 工作流 G：审计事件覆盖清单（15-audit-coverage）

- 审计基线：`main @ 100a93a1382ca310aae588bc3a1734e4d67e94ba`
- 审计对象：`apps/core/models.py AuditEvent`（只追加模型，save 禁更新 / delete 禁删除，`models.py:86-92`）；`AuditEvent.record()` 便捷入口自动采集 request_id/IP/UA（`models.py:94-111`）。
- 对照基准：《PMS2身份认证与员工住客账户体系改造开发方案 2.0》§16.1（`docs/PMS2身份认证与员工住客账户体系改造开发方案2.0.md:851-864`）。

---

## 1. 全仓 AuditEvent.record 已埋点 action 去重清单（生产代码 50 个 action / 53 处调用）

> 统计口径：`grep "AuditEvent.record(" apps hotelapp --include="*.py"`，剔除 test 文件。

| # | action | 触发位置（文件:行号） |
|---|---|---|
| 1 | auth.login_success | hotelapp/views.py:1941 |
| 2 | auth.login_failure | hotelapp/views.py:1874（rate_limited/disabled/bad_credentials/no_employment 四种 reason） |
| 3 | identity.password_changed | hotelapp/views.py:1822（首登/重置后强制改密完成） |
| 4 | identity.password_reset | apps/identity/services.py:381 |
| 5 | identity.employee_create | apps/identity/services.py:123 |
| 6 | identity.role_assign | apps/identity/services.py:256 |
| 7 | identity.role_end | apps/identity/services.py:280 |
| 8 | identity.employment_status | apps/identity/services.py:350 |
| 9 | security.property_context_denied | apps/identity/middleware.py:54 |
| 10 | public.reservation_blocked | apps/api/views.py:164 |
| 11 | public.reservation_created | apps/api/views.py:204 |
| 12 | public.rate_limited | apps/api/views.py:290 |
| 13 | public.reservation_expired | hotelapp/management/commands/release_expired_reservations.py:50 |
| 14 | guest.booking_access_denied | apps/api/views.py:247 / 317 / 418（查询/支付意图/取消三处） |
| 15 | guest.booking_access_viewed | apps/api/views.py:252 |
| 16 | guest.booking_cancelled | apps/api/views.py:437（拒绝）/ 445（成功） |
| 17 | guest.pii_reveal | hotelapp/views.py:1525（旧 Client 身份证揭示） |
| 18 | guest.pii_reveal.document | apps/api/guest_center.py:357 |
| 19 | guest.pii_reveal.phone | apps/api/guest_center.py:386 |
| 20 | guest.document_register | apps/guests/services.py:67 |
| 21 | guest.document_reveal | apps/guests/services.py:84 |
| 22 | guest.create | apps/guests/services.py:136 |
| 23 | guest.update | apps/guests/services.py:179 |
| 24 | guest.phone_add | apps/guests/services.py:205 |
| 25 | member.pii_reveal | hotelapp/views.py:1551（旧 Vip 电话揭示） |
| 26 | membership.create | apps/guests/services.py:244 |
| 27 | membership.level_change | apps/guests/services.py:270 |
| 28 | approval.request | apps/billing/services_v2.py:48 |
| 29 | approval.approve | apps/billing/services_v2.py:83 |
| 30 | approval.reject | apps/billing/services_v2.py:107 |
| 31 | approval.expire | apps/billing/services_v2.py:72 |
| 32 | billing.ledger_post | apps/billing/services_v2.py:205（post_entry 唯一账本写入口） |
| 33 | billing.collect | apps/billing/services_v2.py:274 |
| 34 | billing.deposit_collect | apps/billing/services_v2.py:334 |
| 35 | billing.refund_request | apps/billing/services_v2.py:389 |
| 36 | billing.refund_completed | apps/billing/services_v2.py:465 |
| 37 | billing.reverse_entry | apps/billing/services_v2.py:500 |
| 38 | billing.shift_open | apps/billing/services_v2.py:522 |
| 39 | billing.shift_close | apps/billing/services_v2.py:549 |
| 40 | night_audit.run | apps/billing/night_audit.py:215 |
| 41 | night_audit.reopen | apps/billing/night_audit.py:278 |
| 42 | night_audit.break_glass_requested | apps/billing/night_audit.py:347 |
| 43 | night_audit.break_glass_expired | apps/billing/night_audit.py:376 |
| 44 | night_audit.break_glass_executed | apps/billing/night_audit.py:399 |
| 45 | night_audit.break_glass_reviewed | apps/billing/night_audit.py:444 |
| 46 | inventory.daily_rate_adjust | apps/inventory/services.py:289 |
| 47 | privacy.export_staff | hotelapp/views.py:350（薪酬 CSV 导出执行） |
| 48 | staff.salary_initial_set | hotelapp/views.py:390 |
| 49 | staff.profile_update | hotelapp/views.py:482 |
| 50 | legacy.export | hotelapp/views.py:1498 |

另有 `approval.request` 会覆盖退款/营业日重开/薪酬导出等所有走 ApprovalService 的高危动作申请侧。

---

## 2. 对照方案 §16.1 事件清单：已覆盖 / 部分覆盖 / 缺失

| §16.1 要求事件 | 状态 | 实际落库事件与证据 | 说明 |
|---|---|---|---|
| auth.login_success | **已覆盖** | auth.login_success（hotelapp/views.py:1941） | — |
| auth.login_failure | **已覆盖** | auth.login_failure（hotelapp/views.py:1874），username 仅存 HMAC 指纹 | 符合 §16.2 脱敏 |
| auth.logout | **缺失** | logout_view（hotelapp/views.py:1957）仅 `auth_logout + redirect`，无任何审计 | — |
| auth.password_changed | **部分覆盖** | 首登/重置后强制改密写 `identity.password_changed`（hotelapp/views.py:1822） | 前缀不符（identity. vs auth.）；无"正常登录后自助改密"入口 |
| auth.password_reset | **部分覆盖** | `identity.password_reset`（apps/identity/services.py:381）功能覆盖 | 前缀不符（identity. vs auth.） |
| identity.employee_created | **部分覆盖** | `identity.employee_create`（services.py:123） | 命名单复数偏差；仅建档路径 |
| identity.employee_bound | **缺失（DB 层）** | `provision_account` / `bind_existing_user`（services.py:138-192）只写 logger（services.py:162,189），无 AuditEvent | 开通/绑定账号是高危身份动作 |
| identity.employee_disabled | **部分覆盖** | `identity.employment_status`（services.py:350）覆盖停职/离职/恢复 | 命名偏差 |
| identity.role_assigned | **部分覆盖** | `identity.role_assign`（services.py:256）+ 补充的 `identity.role_end`（services.py:280） | 命名偏差 |
| identity.invitation_created / resent / revoked / accepted | **缺失** | 全仓无邀请（invitation）功能实现；仅 `apps/core/rate_limit.py:30` 预留 `public.invitation_activate` 限流键 | 功能未实现，事件无从谈起 |
| guest.booking_access_viewed | **已覆盖** | apps/api/views.py:252 | — |
| guest.booking_access_denied | **已覆盖** | apps/api/views.py:247/317/418（查询/支付/取消三入口） | — |
| guest.booking_cancelled | **已覆盖** | apps/api/views.py:437/445（拒绝+成功） | — |
| public.reservation_created | **已覆盖** | apps/api/views.py:204 | — |
| public.reservation_blocked | **已覆盖** | apps/api/views.py:164 | — |
| public.rate_limited | **已覆盖** | apps/api/views.py:290 | — |
| security.permission_denied | **缺失** | 全部 403 路径（front_desk/guest_center/night_audit 等 `ApiError("PERMISSION_DENIED")`）均不落 AuditEvent；仅物业上下文拒绝有 `security.property_context_denied`（middleware.py:54） | — |
| security.break_glass_used | **部分覆盖** | `night_audit.break_glass_executed`（night_audit.py:399）语义等价 | 命名偏差 |

小结：§16.1 共 18 个要求事件 → **已覆盖 7**、**部分覆盖 6**（功能有但命名偏差）、**缺失 5**（auth.logout、identity.employee_bound DB 审计、identity.invitation_* 四事件、security.permission_denied）。
命名偏差是系统性问题：同一语义在不同模块用了不同前缀（auth./identity./night_audit.），与 §16.1"事件名统一、稳定"要求不完全一致。

---

## 3. 高危动作 DB 审计缺口盘点

| 高危动作 | DB 审计 | 证据 | 说明 |
|---|---|---|---|
| PII reveal（证件，PMS2） | **有** | apps/api/guest_center.py:357（guest.pii_reveal.document）；apps/guests/services.py:84（guest.document_reveal） | 独立权限 + 强制原因 + 只记脱敏值 |
| PII reveal（电话，PMS2） | **有** | apps/api/guest_center.py:386（guest.pii_reveal.phone） | 与证件分权 |
| PII reveal（旧 Client/Vip 入口） | **有** | hotelapp/views.py:1525 / 1551 | 旧入口仍保留揭示能力且已审计 |
| 退款审批/执行 | **有** | services_v2.py:83（approval.approve）/ 465（billing.refund_completed） | 双人 SoD 全链审计 |
| 夜审执行 | **有** | apps/billing/night_audit.py:215（night_audit.run） | 关账与审计同事务 |
| 重开营业日 | **有** | night_audit.py:278（night_audit.reopen） | 双人审批 |
| break-glass（申请/执行/过期/复核） | **有** | night_audit.py:347/399/376/444 | 全生命周期四事件 |
| 权限矩阵保存 | **无** | apps/identity/permission_admin.py:151 仅 `audit.info` logger | superuser 整体覆盖角色权限（group.permissions.set），属最高危授权变更，无 DB 审计 |
| 前台排房/入住/退房 | **无** | apps/reservations/front_desk_services.py:112/196/340 仅 logger | 房态与账务核心状态变更不可 DB 追溯 |
| 续住/换房/No-Show | **无** | apps/reservations/lifecycle_services.py（三个 @transaction.atomic 服务无任何 AuditEvent.record） | 同上 |
| mock 支付回调（资金确认/过期） | **无** | apps/billing/services.py:74-139 | dev-only，但为 Reservation/Payment/LedgerEntry 三表状态变更 |
| auth.logout | **无** | hotelapp/views.py:1957 | §16.1 明确要求 |
| 员工账号开通/绑定 | **无（DB）** | apps/identity/services.py:162/189 仅 logger | §16.1 identity.employee_bound |
| 预订过期释放（新命令） | **无** | apps/reservations/management/commands/expire_reservation_holds.py（无 record 调用） | 旧命令有 public.reservation_expired（hotelapp/release_expired_reservations.py:50），双链路口径不一 |

**结论：高危动作中"有 DB 审计"7 类、"无 DB 审计"8 类。**

---

## 4. 汇总对比表：动作 | 审计状态 | 证据（文件:行号） | 建议

| 动作 | 审计状态 | 证据 | 建议 |
|---|---|---|---|
| 前台排房 | 缺失 | front_desk_services.py:112（logger） | 在 assign_room 成功分支补 `front_desk.room_assigned` |
| 前台入住 | 缺失 | front_desk_services.py:196（logger） | 补 `front_desk.check_in`（含 stay/room/booking） |
| 前台退房 | 缺失 | front_desk_services.py:340（logger） | 补 `front_desk.check_out`（含 folio 结算结果） |
| 续住 | 缺失 | lifecycle_services.py:52-169 | 补 `front_desk.stay_extend`（before/after check_out、金额） |
| 换房 | 缺失 | lifecycle_services.py:172-250 | 补 `front_desk.room_move`（old/new room、reason） |
| No-Show | 缺失 | lifecycle_services.py:253-320 | 补 `front_desk.no_show`（released_nights） |
| 权限矩阵保存 | 缺失 | permission_admin.py:130-155 | 保存前 diff 前后权限集合并写 AuditEvent（superuser 动作必须落库） |
| auth.logout | 缺失 | hotelapp/views.py:1957 | logout_view 补 `auth.logout` |
| 员工账号开通/绑定 | 仅 logger | identity/services.py:162,189 | provision_account/bind_existing_user 补 `identity.employee_bound` |
| mock 支付回调 | 缺失 | billing/services.py:102-138 | 补 `billing.mock_payment_settled`（至少 SUCCEEDED/EXPIRED 分支） |
| 预订过期（新命令） | 缺失 | expire_reservation_holds.py | 补 `public.reservation_expired` 并与旧命令二选一收敛 |
| 幂等重放/上限 409 | 缺失 | reservations/services.py:68-88 | 低优先：补 public.* 冲突事件 |
| PII reveal（全部 5 个入口） | 已覆盖 | guest_center.py:357/386; guests/services.py:84; hotelapp/views.py:1525/1551 | 保持；建议统一 action 命名（guest.pii_reveal vs guest.pii_reveal.document） |
| 账本写入/收款/押金/退款/冲销/班次 | 已覆盖 | services_v2.py:205-549 | 保持 |
| 夜审四链（run/reopen/break-glass×4） | 已覆盖 | billing/night_audit.py:215-444 | 保持；RUNNING 遗留恢复时补审计 |
| 登录成功/失败 | 已覆盖 | hotelapp/views.py:1941/1874 | 保持（失败已按 HMAC 指纹脱敏） |
| 密码重置/首登改密 | 已覆盖（命名偏差） | identity/services.py:381; hotelapp/views.py:1822 | 统一为 §16.1 auth.* 前缀或修订方案文本 |
| 审批四动作（request/approve/reject/expire） | 已覆盖 | services_v2.py:48-107 | 保持 |
| security.permission_denied | 缺失 | 各视图 ApiError("PERMISSION_DENIED") | 对敏感入口（PII/退款/夜审/矩阵）的 403 补安全审计，避免噪音可仅限高危面 |
| identity.invitation_* | 缺失（功能未实现） | 仅 rate_limit.py:30 预留 | 实现邀请功能时同步埋点；短期在方案中标注"未实现" |

### 量化结论
- 生产代码已埋点 **50 个 action / 53 处调用**；
- §16.1 对照：已覆盖 7 / 部分覆盖 6 / 缺失 5；
- 高危动作 DB 审计缺口 **8 项**（排房、入住、退房、续住、换房、No-Show、权限矩阵保存、auth.logout；另有账号开通 DB 缺失、mock 回调、过期释放 3 项中危缺口，合并口径见 §3）；
- 系统性问题两条：①命名前缀不统一（auth./identity./night_audit./front_desk 未按 §16.1 对齐）；②"DB 审计 + logger"双轨并存且 logger 轨（hotel.audit 1MB×5 轮转）承担了部分本应落库的关键动作，与 §16.2"不能仅依赖 audit.log 轮转"的警示一致。
