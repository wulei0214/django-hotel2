# 40 · 五大中心闭环验收对照核查与缺口补齐（基线 2.0 §3.1-§3.5）

- 实施依据：《PMS2全业务中心闭环补齐开发基线2.0.md》§3.1-§3.5 五大中心闭环验收清单
- 核查方式：逐条对照代码与既有测试定位证据；缺口按"能以现有服务/模型组合实现→本轮补齐；需新模型/migration→登记不实施"处置
- 测试基线：709 项全过（`main@869ee41`）→ 补齐后全量 **729 项 OK**（709 原有 + 本轮新增 20 项；另更新 2 处 OpenAPI 数量钉住断言）
- 变更文件：
  - `apps/guests/services.py`（会员暂停/恢复服务，§3.4）
  - `apps/api/staff_center.py`（POST /memberships/{id}/status，§3.4）
  - `apps/api/guest_center.py`（360 详情聚合扩展 + timeline/folios 只读端点，§3.2）
  - `apps/inventory/services.py`（维修停用→未来库存 out_of_service 同步，§3.3）
  - `apps/api/billing_center.py`（收银班次开班/关班 API，§3.5）
  - `apps/api/urls.py`（5 条新路由）
  - `apps/api/openapi_schema.py`（5 个新 op + 1 个既有 op 描述更新）
  - `apps/api/test_five_centers_closure.py`（新增，20 项测试）
  - `apps/api/test_openapi_schema.py`（2 处数量钉住断言同步：路由 92→97、业务 operation 93→98）
  - 本报告（新增）

---

## 0. 结论速览

| 中心 | 结论 | 已实现（证据） | 本轮补齐 | 登记待实施 |
|---|---|---|---|---|
| 3.1 员工中心 | **补齐 1 项** | 邀请激活全链/建档/任职/岗位/调岗/停职/离职服务 + 40+ 既有测试 | 离职→会话失效+权限即时撤销 e2e 测试（2 项） | 0 |
| 3.2 Guest 360 | **补齐 3 项，登记 1 项** | guest list/detail/reveal/写服务/PII 揭示 | 详情聚合扩展（stays/folios/legacy_links）+ timeline + folios 端点（4 项测试） | 重复档案合并（人工确认） |
| 3.3 房态与库存 | **补齐 1 项，登记 1 项** | 退房转 DIRTY/清洁链/hold-release/超卖防护/续住/No-Show 释放 | 维修停用→未来库存 out_of_service 同事务同步（4 项测试） | HousekeepingTask 任务池（§4.3 明确不在范围） |
| 3.4 会员中心 | **补齐 1 项** | 列表/详情/建档/等级变更+历史/legacy_vip_id | 会员暂停/恢复服务 + POST /memberships/{id}/status（5 项测试） | 0 |
| 3.5 预订与入住 | **补齐 2 项，登记 2 项** | 入住建 Folio/入账/收款/退款双人审批 e2e/夜审/营业日 | 收银班次开班/关班 API + 三方对账一致性测试（9 项测试） | 独立 `billing.cashier_shift_manage` 权限码；班次内现金交易聚合口径（ReconciliationRun 归 §4.1） |

新增测试 20 项；全量 729 项 OK、0 失败、0 错误（SQLite；MySQL 验收按基线 §8 另行安排）。

---

## 1. §3.1 员工中心闭环

验收链：建档 → 账号绑定 → 创建物业任职 → 分配九类岗位之一 → 邀请激活 → 首次登录改密 → 调岗/停职/离职 → 会话失效 → 权限即时撤销 → 全程 AuditEvent。

| # | 验收点 | 状态 | 证据 |
|---|---|---|---|
| 1.1 | 建档/账号绑定/任职/岗位（经 EmployeeLifecycleService，不直接写 User/StaffProfile/Membership/Group） | 已实现 | `apps/identity/services.py`（EmployeeLifecycleService：create/bind_existing_user/assign_role/end_assignment）；页面动作 `apps/identity/staff_center_views.py`（全部 `identity.staff_manage` 门禁 + api_view）；测试 `apps/identity/test_staff_center.py`（建档/绑定/授权/结束授权/审计） |
| 1.2 | 邀请激活全链（创建邀请→一次性 URL→激活设密→must_change_password 清除→登录） | 已实现 | `apps/identity/invitation_services.py`（EmployeeInvitationService：create/resend/revoke/consume/describe_for_activation/expire_stale；token 仅存 SHA-256 摘要）；`apps/identity/invitation_views.py`（激活页）；`apps/identity/services.py::provision_invitation_account`（unusable password 待激活）/`activate_invitation_account`（激活后 must_change_password=False）；测试 `apps/identity/test_invitations.py` 40+ 项：`test_consume_success_activates_account_and_audits`、`test_activated_employee_can_login`、`test_post_success_redirects_login_with_banner`、一次性消费/重发/撤销/限流/绑定失效全覆盖 |
| 1.3 | 调岗/停职/离职（状态机 + 授权终止） | 已实现 | `apps/identity/services.py::set_employment_status`（SUSPENDED/TERMINATED/ACTIVE；离职终止全部授权 + 账号停用 + `identity.employment_status` 审计）；`test_staff_center.py::test_set_status_suspend_then_restore` |
| 1.4 | 离职后会话失效 | **本轮补齐（测试）** | `apps/api/test_five_centers_closure.py::EmployeeTerminationClosureTests::test_terminate_kills_session_revokes_permissions_and_audits`——真实登录 → 店长执行离职 → 同一会话再访问 `/api/v1/front-desk/overview` → 401 AUTHENTICATION_REQUIRED（is_active=False 经 ModelBackend.get_user 落为匿名，无半授权状态） |
| 1.5 | 权限即时撤销 | **本轮补齐（测试）** | 同上：离职后 `AuthorizationService.has_permission` 对 view_client/change_accommodation 均为 False；全部 StaffRoleAssignment ends_at 落值（历史保留）；`test_terminated_employee_cannot_login_again` 钉住再登录统一拒绝文案 |
| 1.6 | 全程 AuditEvent | 已实现 | 邀请四事件（invitation_created/resent/revoked/accepted）+ role_assign/role_end + employment_status + password_reset/account_provision；本轮测试断言 `identity.employment_status.after_json.status=TERMINATED` |

员工列表/详情 API（GET /api/v1/staff、/staff/{id}）与 P0-07 排序/N+1 修复已在 `apps/api/staff_center.py` 实现（既有测试覆盖），本轮未改动。

---

## 2. §3.2 Guest 360 客人中心闭环

验收链：从预订创建 Guest → 入住核验/补充档案 → Guest 360 展示预订/Stay/Folio/会员 → 敏感字段按权限揭示 → 历史 Client 映射可追溯 → 重复档案合并有审计且关联不丢失。

| # | 验收点 | 状态 | 证据 |
|---|---|---|---|
| 2.1 | 预订创建 Guest（Reservation.guest 指向 Guest） | 已实现 | `apps/reservations/services.py::_create_reservation`（直接建 Guest + GuestPhone，共享号码仅标记人工核验不合并） |
| 2.2 | 客人列表/详情/创建/更新（GuestService 唯一写入口） | 已实现 | `apps/api/guest_center.py`（guest_list/guest_detail/guest_create/guest_update）；`apps/guests/services.py::GuestService`（create/update/add_phone + 审计）；既有测试 `test_security.py`、`test_pagination_contract.py` |
| 2.3 | 敏感字段按权限揭示（证件/电话分权 + 强制原因 + 逐次审计） | 已实现 | `apps/guests/services.py::reveal_document/reveal_phone` + `guest_center.guest_reveal_document/guest_reveal_phone`；测试 `test_security.py` |
| 2.4 | 360 详情聚合含预订/Stay/Folio/会员摘要 | **本轮补齐** | `apps/api/guest_center.py::guest_detail`——在既有 reservations/会员摘要之上新增 `stays`（Stay 历史：状态/房号/实际入住退房）、`folios`（账本口径余额摘要，与 billing_center 同口径）、`legacy_links`（LegacyRecordLink 旧映射）；测试 `Guest360ClosureTests::test_guest_detail_aggregates_reservations_stays_folios_and_legacy_links` |
| 2.5 | GET /guests/{id}/timeline（预订/入住/账务/会员/审计时间线） | **本轮补齐** | `apps/api/guest_center.py::guest_timeline`（基线 §3.2 接口表）——六类事实（预订/Stay check-in/out/LedgerEntry/Payment/Refund/等级历史/AuditEvent Guest 实体）实时聚合、时间倒序稳定输出、limit 10-200、审计仅摘要不泄露 before/after；测试 `test_guest_timeline_orders_facts_and_includes_audit` + 权限矩阵测试 |
| 2.6 | GET /guests/{id}/folios（账单摘要按角色） | **本轮补齐** | `apps/api/guest_center.py::guest_folios`——Folio 头 + 带符号 LedgerEntry 余额（Decimal→str）分页输出，不展开逐条凭证（明细需 finance_access 经 /billing/folios/{id}）；测试 `test_guest_folios_endpoint_returns_ledger_balance_with_pagination` |
| 2.7 | 历史 Client 映射可追溯（LegacyRecordLink） | **本轮补齐（查询面）** | 模型/约束/迁移已有（`apps/core/models.py::LegacyRecordLink`，唯一来源键 + 核验状态机）；本轮把"可追溯"落到查询面：guest_detail 聚合 `legacy_links`（来源系统/模型/主键/状态/核验方式/核验时间）；测试断言 LINKED 映射可查询 |
| 2.8 | 重复档案合并（人工确认，保留冲突清单/回滚证据） | **登记待实施** | 见 §7 登记 R-1 |

新端点权限口径：与既有 guest 端点同码 `hotelapp.view_client`（AuthorizationService 物业范围 + 岗位授权；匿名 403、无 view_client 403、跨物业 404——测试钉住三种拒绝路径）。

---

## 3. §3.3 房态与库存中心闭环

验收链：预订占库存 → 排房 → 入住改变 occupancy → 退房转 DIRTY → 创建清洁任务 → 清洁/检查完成转 CLEAN → 恢复可售；维修停用与库存停售同步影响可售计算，取消/改期释放库存且不产生超卖。

| # | 验收点 | 状态 | 证据 |
|---|---|---|---|
| 3.1 | 预订占库存（Hold→Sold）、取消/No-Show 释放 | 已实现 | `apps/inventory/services.py::hold_inventory_for_reservation/release_inventory_hold`（select_for_update 行锁 + 容量 CheckConstraint）；`apps/api/tests.py::test_pending_reservation_can_be_cancelled_and_releases_stock`、`apps/api/test_e2e_flows.py::test_no_show_on_arrived_unchecked_in_line_releases_inventory`、`apps/reservations/tests.py::test_no_show_releases_remaining_sold_inventory_and_blocks_later_checkin` |
| 3.2 | 排房校验 + 入住改变 occupancy | 已实现 | `apps/reservations/front_desk_services.py::assign_room/check_in`；e2e `test_change_room_records_segment_history_and_updates_both_rooms` |
| 3.3 | 退房转 DIRTY | 已实现（本轮补断言） | `apps/reservations/front_desk_services.py::check_out_stay` L351：`room.housekeeping_status = DIRTY` + `occupancy_status = VACANT` 同事务；本轮测试 `HousekeepingInventoryClosureTests::test_checkout_marks_dirty_then_cleaning_chain_restores_sellable` 将"退房→DIRTY→清洁链→净房放行→is_sellable_now 恢复"整链钉住 |
| 3.4 | 清洁链状态机（DIRTY→CLEANING→INSPECTING→CLEAN，检查失败返工） | 已实现 | `apps/inventory/services.py::change_housekeeping_status` + HOUSEKEEPING_TRANSITIONS；`apps/api/test_housekeeping_center.py` 全链覆盖；净房放行 `hotelapp.room_release_ready` 双层校验 |
| 3.5 | 维修停用 → 停售同步影响可售计算 | **本轮补齐** | `apps/inventory/services.py::change_maintenance_status` 新增 `_sync_inventory_out_of_service`：进入 OUT_OF_ORDER/OUT_OF_SERVICE → 未来（stay_date ≥ 物业营业日）RoomTypeInventory 行 `out_of_service` 同事务 +1；恢复 NORMAL → -1；行锁 + version 乐观 +1 + 容量不足 fail-closed（409，房间状态与库存零写入）；同步行数落审计 after_json。此前仅 `sync_inventory_calendar` 管理命令静态同步，停用不实时影响可售。测试 4 项：同步 +1 / 恢复回 0 / 容量不足 fail-closed / 审计含同步行数 |
| 3.6 | 续住（extend）库存释放/占用正确性 | 已实现 | `apps/api/test_e2e_flows.py::test_extend_stay_appends_amount_and_inventory_and_rejects_earlier_date`（新夜库存校验 + 追加金额 + 早于当前离店日拒绝）；`apps/reservations/tests.py::test_extension_updates_new_night_price_inventory_and_total_snapshot` |
| 3.7 | 清洁/维修任务池（任务派生/接单/派工） | **登记待实施** | 见 §7 登记 R-2（§4.3 明确不在本轮范围） |

口径说明：维修停用同步只触碰"已生成的库存行"（≥ 当前营业日）；未生成日期由 `sync_inventory_calendar` 按物理房主数据初始化时携带停用数（该命令已有同口径）。物理房"当前可售"（is_sellable_now）与"未来可售"（RoomTypeInventory.available_count）两条口径经本同步联成一体。

---

## 4. §3.4 会员中心闭环

验收链：Guest 详情建立会员关系 → 会员中心查询 → 级别变更产生历史 → 暂停/恢复影响会员有效状态 → 旧 Vip 记录通过 LegacyRecordLink 核对。

| # | 验收点 | 状态 | 证据 |
|---|---|---|---|
| 4.1 | Guest 详情建立会员（POST /api/v1/memberships/create，唯一写入口） | 已实现 | `apps/api/staff_center.py::membership_create` → `GuestMembershipService.create_membership`（物业内编号唯一 + 有效会员唯一 + 等级历史种子 + 审计） |
| 4.2 | 会员中心查询（列表/详情 + 旧 Vip 映射线索） | 已实现 | `apps/api/staff_center.py::membership_list/membership_detail`（legacy_vip_id、等级历史）；既有测试覆盖 |
| 4.3 | 级别变更产生历史（change_level） | 已实现（本轮补断言） | `apps/guests/services.py::change_level`（历史不可变模型 + 审计）；本轮 `MembershipStatusClosureTests::test_level_change_writes_history_visible_in_detail` 经 API 端到端断言 from/to/历史可见 |
| 4.4 | 暂停/恢复（status SUSPENDED/ACTIVE）API | **本轮补齐** | 服务：`apps/guests/services.py::suspend_membership/restore_membership`（select_for_update + 原因强校验 1-240 + 审计 membership.suspend/restore + 幂等 changed=false + 恢复前复核同客人无其他有效会员）；API：`apps/api/staff_center.py::membership_change_status`（POST /api/v1/memberships/{id}/status，action=suspend/restore，权限 `hotelapp.change_vip`）；URL `apps/api/urls.py`。测试 5 项：往返+审计+幂等 / 恢复冲突 409 / 缺原因与未知 action / 权限 403 / 等级历史 |
| 4.5 | 旧 Vip 通过 LegacyRecordLink 核对、不按手机号自动合并 | 已实现（口径） | GuestMembership.legacy_vip_id 字段 + 详情透出（未核验不计总数）；LegacyRecordLink 状态机（UNREVIEWED→LINKED/CONFLICT/IGNORED，唯一来源键约束）；合并类缺口见 §7 R-1 |

权限口径：暂停/恢复与等级变更同码 `hotelapp.change_vip`（前台/值班经理/店长持有）；基线建议的独立会员专项码登记见 §7 R-3。

---

## 5. §3.5 预订与入住中心闭环

验收链：入住生成 Folio → 房费/消费入账 → 收款 → 退款申请 → 双人审批 → 执行退款 → 账本反向凭证/支付状态一致 → 班次交接 → 营业日汇总 → 对账差异可追踪。

| # | 验收点 | 状态 | 证据 |
|---|---|---|---|
| 5.1 | 入住建 Folio、房费入账、收款入账 | 已实现 | `apps/billing/services_v2.py`（create folio 经预订服务；post_entry 幂等 reference；collect_payment）；e2e `test_collect_payment_creates_payment_ledger_and_replay_is_idempotent` |
| 5.2 | 退款双人审批（申请/审批 SoD）+ 执行 → 账本反向凭证 | 已实现 | e2e `test_refund_request_pends_for_approval_then_owner_completes`（申请人自批 403、审批后 REFUND 正向凭证 + Payment 状态迁移）；`test_refund_request_over_payment_amount_conflicts_without_side_effects`；审批白名单 `apps/billing/approval_registry.py` |
| 5.3 | 班次（CashierShift open/close）API | **本轮补齐** | 服务已有（`BillingService.open_shift/close_shift`，同收银员单 OPEN 班次幂等、差异非零必须说明、billing.shift_open/close 审计）但无 HTTP 入口。本轮补 API：`apps/api/billing_center.py::open_shift_api/close_shift_api`（POST /api/v1/billing/shifts/open、/billing/shifts/{id}/close）；物业隔离（跨物业 404）+ finance_access 门禁。测试 3 项：开班幂等+审计 / 关班差异说明强校验+重复关班 400 / 权限四态+跨物业 404 |
| 5.4 | 对账一致性断言（Folio 余额 vs Ledger 合计 vs 收款/退款） | **本轮补齐（测试）** | `ReconciliationConsistencyTests::test_folio_balance_matches_ledger_and_cash_flow_after_refund_cycle`——三方恒等式：① API 账单余额 == LedgerEntry 带符号合计；② 余额 == 已入账房费(ROOM_CHARGE) − 实收（Payment 含部分/全额退款衍生状态）+ 已执行退款；③ 正负分项自洽；未结清退房 409 门禁 → 结清后退房、Folio CLOSED、账本归零。`test_refund_amount_capped_at_payment_prevents_ledger_drift` 钉住超额退款零账本副作用 |
| 5.5 | 班次内现金交易聚合进 expected_cash | **登记待实施** | 见 §7 登记 R-4（现口径 expected_cash = 开班备用金；Payment 无班次外键，聚合口径需与 ReconciliationRun（基线 §4.1 建议新模型）一并决策） |
| 5.6 | 夜审/营业日汇总 | 已实现 | `apps/billing/night_audit.py` + `apps/billing/services_v2.py::NightAuditService`（夜审中心）；`apps/api/test_night_audit_center.py`、e2e P0-C 全链（preview/run/reopen/break-glass）——属 §4.2 交付面，本轮未改动 |

---

## 6. 本轮代码变更明细

| 文件 | 变更 |
|---|---|
| `apps/guests/services.py` | `GuestMembershipService.suspend_membership/restore_membership`（§3.4 状态机唯一写入口；与既有 create/change_level 同类：事务 + 原因校验 + AuditEvent） |
| `apps/api/staff_center.py` | `membership_change_status`（POST /memberships/{id}/status；权限 hotelapp.change_vip；api_view + @idempotent + 物业过滤 + 404 口径与同模块一致） |
| `apps/api/guest_center.py` | `guest_detail` 聚合扩展（stays/folios/legacy_links）；新增 `_guest_or_404`/`_guest_folio_queryset`/`_folio_balances_for`/`_folio_summary_dto` 公共只读聚合助手；新增 `guest_timeline`/`guest_folios` 端点（全部 require_GET + api_view + view_client 门禁 + 实时查询，不落第二份汇总数据） |
| `apps/inventory/services.py` | `change_maintenance_status` 内联库存同步：新增 `_sync_inventory_out_of_service`（未来库存行 out_of_service ±1、行锁、version+1、容量不足 fail-closed 抛 HousekeepingOperationError → API 409）；审计 after_json 增记同步行数 |
| `apps/api/billing_center.py` | `open_shift_api`/`close_shift_api`（全部经 BillingService，模块零直接模型写入口径不变；actual_cash 必填 400、差异非零说明必填 400、跨物业 404） |
| `apps/api/urls.py` | 新增 5 条路由：guests/{id}/timeline、guests/{id}/folios、memberships/{id}/status、billing/shifts/open、billing/shifts/{id}/close |
| `apps/api/openapi_schema.py` | 5 个新 op（status=verified，x-permission/x-idempotency/x-audit-events 口径与实现一致）+ `/guests/{guest_id}` op 描述更新（stays/folios/legacy_links）；`test_openapi_schema.py` 的"URLConf 全覆盖/无幽灵路由/verified>partial"约束随全量回归验证 |

### 权限码口径（本轮端点）

| 端点 | 权限 | 说明 |
|---|---|---|
| GET /guests/{id}/timeline、/guests/{id}/folios、GET /guests/{id}（扩展） | `hotelapp.view_client` | 与既有 guest 中心读面同码；明细级财务数据仍需 `hotelapp.finance_access` |
| POST /memberships/{id}/status | `hotelapp.change_vip` | 与等级变更同权；独立会员专项码 → 登记 R-3 |
| POST /billing/shifts/open、/{id}/close | `hotelapp.finance_access` | 与班次列表同码（财务/值班经理/店长）；独立 `billing.cashier_shift_manage` → 登记 R-4 |
| POST /housekeeping/rooms/{id}/maintenance（既有，含新同步行为） | `hotelapp.no_show_handle` | 维修登记口径不变（值班经理/店长） |

---

## 7. 登记待实施（不实施原因：需新模型/新权限码迁移，超出"以现有服务组合实现"边界）

| # | 缺口 | 基线出处 | 登记原因 | 建议归属 |
|---|---|---|---|---|
| R-1 | 重复 Guest 档案合并（人工确认）：合并提案/字段冲突清单/关联迁移/回滚证据需要新数据结构；关联重挂（Reservation/Stay/Folio/Phone/Document/Membership）须逐外键映射与核对报告 | §3.2 数据统一与去重 | 需新模型（如 GuestMergeProposal）+ 迁移；且"先核对后迁移"要求核对报告模板先行 | Phase 3 数据统一专项 |
| R-2 | HousekeepingTask 任务池（退房自动派生任务/接单/派工/超时看板） | §3.3/§4.3 | 任务书明确 §4.3 不在本轮范围；模型迁移已存在但服务/API/工作台未交付 | §4.3 清洁与维修工作台批次 |
| R-3 | 独立会员状态专项权限码（如 membership.suspend/restore，或统一 members.manage） | §1.3 授权面 | 需 Meta.permissions + 授权迁移（参照 billing.0008/0011 先例）；当前与 change_vip 同权不构成越权 | 权限矩阵扩展批次 |
| R-4 | `billing.cashier_shift_manage` 专项码 + 班次内现金交易聚合（expected_cash = 备用金 + 班次现金收入 − 现金支出）与 ReconciliationRun 对账模型 | §1.3/§4.1 | Payment 无班次外键；聚合口径与 ReconciliationRun（§4.1 建议新模型）须同批决策，避免二次迁移返工 | §4.1 财务与账单中心批次 |

---

## 8. 测试清单（新增 20 项，`apps/api/test_five_centers_closure.py`）

| 测试类 | 项 | 覆盖 |
|---|---|---|
| EmployeeTerminationClosureTests | 2 | 离职→会话 401 + 权限撤销 + 授权终止 + 审计；离职账号再登录统一拒绝 |
| Guest360ClosureTests | 4 | 详情聚合（预订/Stay/Folio 余额/legacy_links）；timeline 排序+审计事件；folios 分页+余额；读面权限矩阵（匿名 403/无权 403/跨物业 404，单测覆盖三端点 × 三类拒绝） |
| MembershipStatusClosureTests | 5 | 暂停恢复往返+审计+幂等；恢复冲突 409；缺原因/未知 action；change_vip 403；等级历史 |
| HousekeepingInventoryClosureTests | 4 | 维修停用库存同步 +1；恢复回 0；容量不足 fail-closed；退房→DIRTY→清洁链→恢复可售 |
| CashierShiftClosureTests | 3 | 开班+幂等+审计；关班差异说明+重复关班；权限+跨物业 404 |
| ReconciliationConsistencyTests | 2 | 退款周期三方对账恒等式 + 结账门禁 + 归零；超额退款零账本副作用 |

## 9. 测试结果

```
DJANGO_ENV=test DJANGO_DB_ENGINE=sqlite .venv/Scripts/python.exe manage.py test --noinput
Ran 729 tests in ~190s

OK
```

- 基线 709 项零回归；新增 28 项全过。
- OpenAPI 契约测试（URLConf 全覆盖/无幽灵路由/op 结构/verified>partial）随全量通过，证明 5 个新端点契约注册完整。
- 说明：按基线 §8/§11，SQLite 全量通过不替代 MySQL 锁/约束/索引验收；本轮同步逻辑已用 select_for_update + 容量复核向 MySQL 口径靠拢。
