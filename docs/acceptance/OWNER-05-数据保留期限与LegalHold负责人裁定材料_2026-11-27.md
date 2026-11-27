# OWNER-05 数据保留期限与 Legal Hold 负责人裁定材料（2026-11-27）

> 文档编号：PMS2-OWNER05-DECISION-001 ｜ 类型：ACCEPTANCE ｜ 状态：ACTIVE ｜ 版本域：PMS2.0 ｜ 业务模块：数据治理 / 隐私合规
> 任务编号：TASK-13 ｜ 交付物：DEL-12
> 执行日期：2026-11-27 ｜ 代码基线：main HEAD `0fa7086`（与本报告同时取证）
> 执行方式：只读取证（Read / 限定 grep / `git rev-parse`），未运行任何写库命令、未跑 MySQL 全量测试、未修改任何 `.py` 代码
> 任务依据：《PMS 2.0 最终验收补证、遗留事项整改与 PMS 3.0 启动准备任务书》PMS2-FINAL-ACCEPTANCE-REMEDIATION-003 V1.0 §TASK-13（第 483–521 行）
> 关联文档（只读，未修改）：`docs/acceptance/OWNER-05-06裁定落实与执行边界记录_2026-11-25.md`、`docs/acceptance/PROD-01至08与M5-4依赖状态表_2026-11-26.md`

---

## 一、结论摘要

| # | 结论 | 状态 | 关键证据 |
|---|---|---|---|
| C-1 | 六类数据（身份/业务/财务/审计/系统日志/临时）的**真实表名、关键字段、文件:行号已现取现核**，每类 10 个字段全部给出「建议 + 依据」，全部标注未获批 | `PASS`（分类与建议口径） / `WAITING_OWNER`（期限与责任人） | 本报告 §二 各表；表名取自 `apps/*/models.py` 的 `class ...(models.Model)` 限定 grep |
| C-2 | **保留期限完全数据驱动、代码零硬编码**：期限值由 `RetentionPolicy.retention_days` 数据行提供，负责人确认后写数据即可生效，无需改代码 | `PASS` | `apps/guests/models.py:509-511`、`apps/guests/models.py:495-498`、`apps/guests/models.py:519-527`、`apps/guests/models.py:554-556` |
| C-3 | **Legal Hold 在现有到期清理路径上确实能阻断**：ACTIVE 冻结对象计入 `skipped` 而非失败；dry-run 与正式执行**各自重算**冻结集合，审批后才下达的冻结同样阻断执行 | `PASS_WITH_RESERVATIONS` | `apps/guests/retention.py:69-77`（重算）、`apps/guests/retention.py:143`（每次调用重算）、`apps/guests/retention.py:162-165`（跳过判定） |
| C-4 | **Legal Hold 存在 4 项实现缺口**（不实施修复，仅列建议）：G-1 无产品化施加入口、G-2 证件/会员/员工/旧表无冻结阻断点、G-3 逐条处理循环内不复查冻结、G-4 冻结后仍可写策略配置 | `PASS_WITH_RESERVATIONS` | 见本报告 §二.7 |
| C-5 | **全仓不存在「到期即自动清理」的触发器**：保留期清理只有 API 手工入口，无管理命令、无调度器 → 保留期限未定**不会**导致生产数据被自动删除 | `PASS` | `apps/api/privacy_center.py:73`、`:194`（唯一非测试调用点）；`apps/*/management/commands/` 实测 33 个命令模块中无保留期清理命令 |
| C-6 | **生产真实批量匿名化与物理删除继续冻结** | `PRODUCTION_PENDING` | `apps/api/privacy_center.py:10-11`；`docs/acceptance/OWNER-05-06裁定落实与执行边界记录_2026-11-25.md:23-26`；`docs/acceptance/PROD-01至08与M5-4依赖状态表_2026-11-26.md:85` |
| C-7 | **历史结论已部分失效（须以本文为准）**：2026-11-25 记录称「`plan_retention_cleanup` 全仓无 API 端点调用…生产清理无任何启用路径」（`OWNER-05-06裁定落实与执行边界记录_2026-11-25.md:38`），在 `0fa7086` 基线已**不成立**——三条 API 路由已落地 | `PASS`（失效声明） | `apps/api/privacy_center.py:52`、`:93`、`:164`；`apps/api/urls.py:342-356`；`hotel/urls.py:60` |
| C-8 | 仓库内**同一主题存在两处互相冲突的留存口径**：审计留存 10 年（已裁定）vs 审计留存「待确认、不设固定年限」（RETENTION_NOTE） | `WAITING_OWNER` | `apps/api/audit_governance.py:48-53` vs `apps/api/audit_center.py:40-45` |
| C-9 | **发票（Invoice）数据载体在本仓库不存在**，最接近的法定凭证载体是 `LedgerEntry.reference`（凭证号） | `PASS`（缺口登记） | `rg -n "class Invoice\|db_table = 'invoice'\|Invoice" --glob "*.py"` 零命中；`apps/billing/models.py:330-335` |
| C-10 | 本轮**未修改任何 `.py`**，未执行任何数据写入/删除动作 | `PASS` | 本报告 §三 复核方法 |

---

## 二、六类数据分类与保留期限建议

> **口径声明（红线）**
> 1. 下表所有「建议保留期限」均为**开发侧建议值**，**不是**已批准期限。保留期限与法律责任人**由负责人确认**，本报告状态一律 `WAITING_OWNER`。
> 2. 「法律或合同依据」列只填**依据类型**与**仓库内可核实的既有裁定**；本仓库内**不存在**任何法条条文号的现取记录——`retention_days` 模型定义无 default 字面量（`apps/guests/models.py:509-511`），全仓年限字面量仅出现在测试文件（见 §四 U-1 复核方法），故**不预填条文号**，条文号由法律责任人填写。
> 3. 与 `docs/acceptance/OWNER-05-06裁定落实与执行边界记录_2026-11-25.md:42` 的差异：该记录为避免「默认值伪装成已批准期限」而把建议期限**留空**；本任务书明确要求「每类必须给建议保留期限」，故本文给出**显式标注为未获批的建议值**，并在每格保留 `WAITING_OWNER` 标记。两种做法不冲突：本文所有建议值**不得**被当作已批准期限渲染或引用。
> 4. 表名口径：Django 默认 `db_table = <app_label>_<model_name 小写>`；`apps/` 下仅 `apps/billing/models.py` 的 7 个归档模型显式覆盖 `db_table`（`legacy_*_archive`，见 `apps/billing/models.py:2672`、`:2693`、`:2716`、`:2761`、`:2809`、`:2859`、`:2904`），旧 `hotelapp` 模型表名见其 `Meta.db_table`。

---

### 2.1 身份信息（Identity）

| 字段 | 内容 |
|---|---|
| **数据范围** | ① `guests_guest` — 住客主档（`apps/guests/models.py:9`）：`full_name`(23)、`phone_ciphertext`(40)、`phone_hash`(46)、`email`(54)、`notes`(55)、`consent_version`(56)、`merged_into`/`merged_at`(66/75)、**姓名密文 `full_name_ciphertext`(88)**、**姓名盲索引 `full_name_hmac`(94)**、`pii_key_version`(102)、历史明文列 `phone`/`phone_normalized`(24/32)<br>② `guests_guestphone` — 多条电话与历史（`apps/guests/models.py:132`）<br>③ `guests_guestdocument` — 证件登记（`apps/guests/models.py:243`）：`doc_type`(263)、`doc_no_ciphertext`(266)、`doc_no_hash`(267)、`issuing_country`(270)、`expiry_date`(273)、`verified_at`/`verified_by`(274/275)、`retention_until`(283)、`is_primary`(286)<br>④ `guests_guestdocumentindex` — 证件占用索引（`apps/guests/models.py:313`）<br>⑤ `guests_guestmembership` — 会员关系（`apps/guests/models.py:358`）：`member_no`(377)、`level_code`(380)、`expires_at`(393)、`legacy_vip_id`(397)<br>⑥ `guests_membershiplevelhistory` — 等级历史（`apps/guests/models.py:445`）<br>⑦ `identity_staffprofile` — 员工自然人档案（`apps/identity/models.py:11`）：`full_name`(39)、`phone`(40)、`employment_status`(41-46)、`must_change_password`(47)、`last_password_change`(52)<br>⑧ `identity_propertystaffmembership` — 任职（`apps/identity/models.py:68`）<br>⑨ 旧表 `client`（`hotelapp/models.py:77`，`db_table='client'` :94）**明文** `姓名`(79)/`身份证号`(80)；旧表 `staff`（`hotelapp/models.py:287`，`db_table='staff'` :322）**明文** `姓名`(296)/`月薪`(300)/`工龄`(302) |
| **业务用途** | 自然人识别与去重（`phone_hash` HMAC 检索 / `full_name_hmac` 盲索引精确检索，见 `apps/guests/models.py:12-14`、`:94-101`）；关联预订、账单、会员、证件；员工身份与任职授权基础 |
| **法律或合同依据** | 依据类型：主档=**正当业务目的（合同履行必要）**，证件=**法律法规要求**，员工=**合同义务 + 法律法规要求**（`RetentionPolicy.RetentionBasis` 三枚举：`apps/guests/models.py:495-498`）。<br>⚠ **具体条文号仓库内无现取记录 → 未核实**，由法律责任人填写 `RetentionPolicy.basis_note`（`apps/guests/models.py:515-517`，字段上限 240 字符） |
| **建议保留期限**（`WAITING_OWNER`） | 住客主档：**建议 10 年**，与仓库内唯一已裁定的留存口径对齐（审计事件最低留存 10 年，「与财务凭证/账务事实留存口径一致」，`apps/api/audit_governance.py:48-53`）——住客主档是账务关联锚点，短于账务期限将导致无法自证。<br>住客证件：**建议 ≥ 财务凭证留存期**（同上 10 年口径）或负责人另行指定的实名登记留存期；仓库内无该口径现取依据 → 未核实。<br>员工档案：**建议 10 年**（员工操作是审计事件 actor，`AuditEvent.actor` PROTECT 外键，`apps/core/models.py:89-97`）。<br>旧表 `client`/`staff`：**不单独设期限**，随 M5-4 归档批次处置（`PROD-01至08与M5-4依赖状态表_2026-11-26.md:57-59`） |
| **起算时间** | 住客主档：**最后一笔预订的离店日**（`Reservation.check_out` 取 Max），无任何预订历史则按 `Guest.created_at.date()`（`apps/guests/retention.py:99-101`、`:120`）。<br>证件：失效日 `retention_until`（`apps/guests/services.py:666-670`）。<br>会员：`expires_at` 或状态终止日（`apps/guests/models.py:365`、`:393`）。<br>员工：`employment_status=TERMINATED` 之日。旧表：归档批次执行日 |
| **到期处理方式** | 住客主档：**不可逆匿名化**——`full_name` 置固定标记「已匿名化（保留期满清理）」（`apps/guests/retention.py:39`），抹除全部 PII 密文/哈希，**保留行本身**以维持 `Reservation`/`Folio` 等 PROTECT 外键完整性（`apps/guests/retention.py:10-12`、`:308-337`）。<br>证件：**当前无任何到期清理实现**（缺口 G-2）→ 建议「置 `retention_until` + 抹除 `doc_no_ciphertext`，保留 `doc_no_hash` 索引以维持唯一占用」，待裁定。<br>会员：状态置 `EXPIRED`（已有 `apps/guests/management/commands/expire_memberships.py`）。<br>员工档案：档案行保留（审计外键 PROTECT），仅脱敏 `phone`。<br>旧表：不清理（冻结） |
| **Legal Hold 影响** | `target_type="GUEST"` 的 ACTIVE 冻结**阻断住客主档匿名化**（`apps/guests/retention.py:69-77`、`:162-165`）。<br>证件 / 会员 / 员工 / 旧表：**无冻结阻断点**（当前无清理路径可阻断；缺口 G-2）。冻结记录自身不可删除、事实不可改（`apps/guests/models.py:654-685`） |
| **责任岗位** | 申请：`guests.retention_request`（申请人：财务/店长，`apps/identity/registry.py:513-520`）；审批：`guests.retention_approve`（值班经理/店长，`:522-529`）；执行：`guests.retention_execute`（值班经理/店长，申请人≠审批人≠执行人，`:531-538`）；**法律责任人：待负责人指定 → `WAITING_OWNER`** |
| **删除或匿名化执行方式** | 服务层 `anonymize_guest(guest)`（`apps/guests/retention.py:308-346`）：字段级抹除（`full_name`→标记、`phone_ciphertext`/`phone_hash`→NULL、`email`/`notes`/`consent_version`→空串、历史明文列→NULL）+ 行保留；每次落 `AuditEvent`（`action="privacy.guest_anonymized"`，`:338-345`）。<br>幂等：已匿名化对象直接返回 False（`:314-315`），且候选集排除标记值（`:106`）。<br>执行门（三层缺一不可）：① 审批真实有效（APPROVED + 同物业 + 同动作码，`:207-214`）② 计划绑定四重一致性（物业/策略/基准日/计划实体，`:216-242`）③ 独立执行权限 + SoD（`:250-260`）。<br>API 入口：`POST /api/v1/privacy/retention/plans`（`apps/api/privacy_center.py:52-90`）→ `/approve`（`:93-161`）→ `/execute`（`:164-213`），路由 `apps/api/urls.py:342-356`，前缀 `hotel/urls.py:60` |
| **备份数据处理规则** | 备份介质中已到期数据**不单独就地擦除**（成本与可审计性不划算）；随备份介质整体到期销毁。恢复后须**重跑同一保留期清理流程**（幂等，`apps/guests/retention.py:314-315`）。<br>PII 密文受密钥生命周期支配：生产密钥轮换（PROD-02）后备份中的 PII 不可解，需与密钥托管同步处置（`hotel/settings.py:36`，`DJANGO_PII_KEY`）。<br>备份恢复演练未验收 → `PRODUCTION_PENDING`（`PROD-01至08与M5-4依赖状态表_2026-11-26.md:34`；仓库内 `scripts/pii_restore_drill.py`、`scripts/pii_restore_drill_mysql.py` 为**本地/合成**演练，不构成生产证据，同文件 `:34`、`:79`） |

---

### 2.2 业务数据（Business）

| 字段 | 内容 |
|---|---|
| **数据范围** | ① `reservations_reservation` — 预订主单（`apps/reservations/models.py:12`）：`booking_no`(30)、`guest` FK PROTECT(36-41)、`status`(48-53)、`billing_type`(61-68)、`check_in`/`check_out`(69-73)、`room_count`/`adult_count`/`child_count`(74-76)、`total_amount`/`deposit_amount`(77-89)、`idempotency_key`(91)、`expires_at`(104)<br>② `reservations_reservationroom`（`apps/reservations/models.py:156`）③ `reservations_stay` — 实际入住（`apps/reservations/models.py:264`）：`room`(284-289)、`actual_check_in`/`actual_check_out`(290-291)、`status`(292-296)<br>④ `reservations_stayroomsegment`(313)、`reservations_noshowrecord`(383)、`reservations_reservationguest`(418)、`reservations_stayguest`(498)<br>⑤ `reservations_inventoryhold` — 库存冻结明细（`apps/reservations/models.py:542`）：`stay_date`(563)、`quantity`(571)、`status`(572)、`expires_at`(583)、`released_at`(585)<br>⑥ `reservations_pendingbookinggate`(831)、`reservations_cancellationpolicy`(626)、`reservations_depositpolicy`(674)<br>⑦ 房态：`inventory_room`（`apps/inventory/models.py:51`）三状态维度 `occupancy_status`(87)/`housekeeping_status`(58)/`maintenance_status`(64)、`inventory_roomtype`(7)、`inventory_dailyrate`(191)、`inventory_roomtypeinventory`(262)<br>⑧ 旧表 `accommodation`（`hotelapp/models.py:46`，`db_table='accommodation'` :74，`联系电话`/`收款金额`/`押金` 明文 :54-55）、旧表 `order`（`hotelapp/models.py:121`，`db_table='order'` :146） |
| **业务用途** | 预订受理与履约、房间分配与房态、入住/退房事实、库存占用与释放、客房清洁与维修工单（`inventory_housekeepingtask` :335、`inventory_maintenanceticket` :436）、账单头（`billing_folio`） |
| **法律或合同依据** | 依据类型：**合同义务**（预订合同/入住协议）+ **正当业务目的**（运营履约、房态管理）。合同编号/条款模板在仓库内**无现取记录** → 未核实 |
| **建议保留期限**（`WAITING_OWNER`） | **建议 10 年**（与财务/审计已裁定的 10 年口径对齐，`apps/api/audit_governance.py:48-53`），理由：业务数据是财务凭证与审计事件的**关联源**，其留存须 **≥** 关联财务/审计留存期，否则账务与审计将失去可回溯的业务事实。<br>库存冻结/待付闸门等**中间态**数据：建议短于业务主数据，详见 §2.6 临时数据 |
| **起算时间** | 预订/订单：退房日 `check_out`（`apps/reservations/models.py:70-73`）；在住记录：`actual_check_out`（`:291`）；房态/工单：业务发生日（`created_at`） |
| **到期处理方式** | **业务数据不进入清理范围**——`apps/guests/retention.py:7-9` 明文「不因 Guest 主档到期而级联删除财务账务（Folio/LedgerEntry/Payment 与 AuditEvent 永不在清理范围——依法保存）」；`Reservation.guest` 为 PROTECT 外键（`apps/reservations/models.py:36-41`），物理删除 Guest 天然受阻。<br>建议到期后**只读冷存**（不删行、不匿名化），确需销毁走 M5-4 归档流程并先取得 L4 书面审批（`PROD-01至08与M5-4依赖状态表_2026-11-26.md:59`） |
| **Legal Hold 影响** | 无直接阻断点（清理路径只作用于 Guest 主档）。间接效应：Guest 被冻结 → 其预订/账单同样处于保全范围，且未结束预订、未结 Folio、未完成退款的对象**根本不会进入候选集**（`apps/guests/retention.py:80-93`、`:42-51`），即业务侧本就「冻结优先」 |
| **责任岗位** | 业务数据**不参与**清理执行，责任岗位为运营负责人（保留期只读登记与到期冷存判定）；清理执行仍走 §2.1 的申请/审批/执行三岗 |
| **删除或匿名化执行方式** | **不执行删除、不执行匿名化**。仅在 M5-4 归档流程获批后按批次处置（当前归档前置 8 项未满足，其中 M5-4-P3「L4 数据删除审批」未获，`PROD-01至08与M5-4依赖状态表_2026-11-26.md:57-70`） |
| **备份数据处理规则** | 业务数据与财务数据同批次备份、同介质到期策略。恢复后**不重跑**保留期清理（该流程只作用于 Guest 主档）。备份中含已退房业务的记录在介质销毁时一并销毁，销毁须有恢复演练证据（PROD-03 未验收，`PROD-01至08与M5-4依赖状态表_2026-11-26.md:34`） |

---

### 2.3 财务数据（Financial）

| 字段 | 内容 |
|---|---|
| **数据范围** | ① `billing_folio` — 账单户头（`apps/billing/models.py:146`）：`folio_type`(181)、`master_reservation_key`(184)、`guest`(187)、`status`(193-198)、`currency`(199)、`opened_at`/`closed_at`(200-201)<br>② `billing_ledgerentry` — **不可变账本凭证**（`apps/billing/models.py:279`）：`entry_type`(313)、`amount`(319-324)、`currency`(325)、`business_date`(326-329)、**`reference` 凭证号（唯一）**(330-335)、`description`(336)、`posted_by`(337-344)、`void_of`/`void_reason`(345-359)、`approval_record`(360-368)<br>③ `billing_payment` — 支付流水（`apps/billing/models.py:439`）：`provider`(481-486)、`provider_ref`(487-492)、`method`(493)、`amount`(494-499)、`status`(501-506)、`paid_at`(507)<br>④ `billing_refund` — 退款三段事实（`apps/billing/models.py:654`）：`approval_record`(672)、`payment`(680)、`amount`(695)、`reason`(697)、`provider_ref`(699)、`status`(700-705)、`requested_by`/`approved_by`/`executed_by`(706-727)、`executed_at`(728)<br>⑤ `billing_refundliability`(1068)、`billing_cashiershift`(1289)、`billing_cashiershiftmovement`(1485)、`billing_chargeline`(1580)、`billing_taxline`(1629)、`billing_billingadjustment`(1648)、`billing_foliotransfer`(235)、`billing_paymentintent`(1891)、`billing_providertransaction`(2025)、`billing_providertransactionevent`(2201)、`billing_paymentcompensationtask`(2286)、`billing_folioreconciliationrun`(2347)、`billing_folioreconciliationdiff`(2379)<br>⑥ **发票：无独立数据载体**——全仓 `rg -n "class Invoice\|db_table *= *['\"]invoice"` **零命中**（`invoice` 全仓仅命中 `hotel/settings.py:337` 的菜单图标）。最接近的法定凭证载体是 `billing_ledgerentry.reference`（`apps/billing/models.py:330-335`）与 `billing_taxline`；若负责人需要独立发票表，须另立开发项 |
| **业务用途** | 账务事实、收款与退款、税额、班次对账与差错更正；支撑 PMS3.0 财务与经营报表 |
| **法律或合同依据** | 依据类型：**法律法规要求**（会计与税收征管口径）。仓库内**唯一可引用的既有裁定**：「审计事件最低留存 **10 年**，与财务凭证/账务事实留存口径一致；到期销毁须走独立审批与只读备份」（`apps/api/audit_governance.py:48-53`）。⚠ 会计法/税收征管法的**具体条文号仓库内无现取记录 → 未核实**，由法律责任人填写 `basis_note` |
| **建议保留期限**（`WAITING_OWNER`） | **建议 ≥ 10 年**，直接沿用仓库内已裁定口径（`apps/api/audit_governance.py:49` `AUDIT_RETENTION_YEARS = 10`）与「与财务凭证/账务事实留存口径一致」的表述（`:51`）。<br>发票（若确认需要独立载体）：建议与账本凭证同期限，且以 `business_date` 为准 |
| **起算时间** | 账本凭证：`business_date`（夜审营业日口径，`apps/billing/models.py:326-329`）；支付：`paid_at`（`:507`）；退款：`executed_at`（`:728`）；账单户头：`closed_at`（`:201`） |
| **到期处理方式** | **代码层禁止删除与修改**：`LedgerEntry.delete()` 抛 `RuntimeError`（`apps/billing/models.py:435-436`）、`save()` 拒绝改已存在行（`:422-433`）、`ImmutableLedgerEntryQuerySet` 的 `delete`/`update`/`bulk_create`/`bulk_update` 全部抛错（`:265-276`）；`Payment`/`Refund`/`ApprovalRecord` 同用 `ImmutableFinancialFactQuerySet`（`:442`、`:657`、`:32`），`ApprovalRecord.delete()` 抛错（`:139-140`）。<br>→ 到期后**唯一合法动作是「只读冷存 + 到期销毁（销毁介质整体）」**，且销毁须走独立审批与只读备份（`apps/api/audit_governance.py:52`）。错账只能用**红字冲销**表达（`apps/billing/models.py:422-433`） |
| **Legal Hold 影响** | **无影响**——财务数据**永不在清理范围**（`apps/guests/retention.py:7-9`），Legal Hold 无需阻断即天然不受影响。反向约束：Legal Hold 期间**更不得**做归档/冷存/销毁（保全要求覆盖全部处置动作） |
| **责任岗位** | 财务负责人（账务事实唯一责任人）；红字冲销双人控制：`billing.reverse_entry`（申请/执行）+ `billing.reversal_approve`（值班经理/店长审批），权限声明见 `apps/billing/models.py:412-417`。**法律责任人：待负责人指定 → `WAITING_OWNER`** |
| **删除或匿名化执行方式** | **不执行删除、不执行匿名化**（代码层已强制）。到期销毁需负责人书面授权 + 独立审批记录 + 只读备份留存（仓库内**未发现**到期销毁执行器 → 未核实，需另立开发项） |
| **备份数据处理规则** | 财务数据随生产备份留存，介质保留期 **≥** 数据保留期（否则介质先于数据到期即构成账务凭证缺失）。恢复演练须核对「表数/用户数/物业数」与 RPO 15min / RTO 4h（`PROD-01至08与M5-4依赖状态表_2026-11-26.md:34`、`:58`），当前 `WAITING_FOR_DBA` |

---

### 2.4 审计数据（Audit）

| 字段 | 内容 |
|---|---|
| **数据范围** | ① `core_auditevent` — **不可变审计事件**（`apps/core/models.py:72`）：`property`(81-88)、`actor`(89-97)、`action`(98-102)、`entity_type`/`entity_id`(103-104)、`before_json`/`after_json`(105-110)、`reason`(111)、`request_id`(112)、`nonce`(113-120)、`ip`(121)、`user_agent`(122)、`created_at`(123)<br>② `billing_approvalrecord` — **不可变高危审批事实**（`apps/billing/models.py:29`）：`action_code`(47)、`entity_type`/`entity_id`(48-49)、`requested_by`/`approved_by`(50-63)、`status`(64-66)、`reason`(67)、`payload_snapshot`(68)、`idempotency_key`(69)、`expires_at`(71)<br>③ `core_exportcredential` — 受控导出凭证（`apps/core/models.py:896`）：`kind`(917-919)、`token`(923)、`params`/`columns`(924-925)、`row_count`(926)、`reason`(927)、`expires_at`(936)<br>④ `core_legacyrecordlink`(191)、`core_statustransition`(524)、`core_breakglassrecord`(353)、`core_nightauditrun`(426)、`core_workitem`(762) |
| **业务用途** | 合规追溯与举证；双人控制证据链；数据导出留痕与「谁在何时以何口径导出过」；夜审与破窗操作留痕 |
| **法律或合同依据** | 依据类型：**法律法规要求**。仓库内已裁定：审计事件最低留存 **10 年**（负责人 2026-11-17 裁定认可），与财务凭证/账务事实留存口径一致，到期销毁须走独立审批与只读备份（`apps/api/audit_governance.py:48-53`）。<br>⚠ **同库口径冲突**：`apps/api/audit_center.py:40-45` 的 `RETENTION_NOTE` 仍写「保留期限为待法务/业务负责人确认项，当前不设定固定年限」——与上述已裁定值矛盾。该接口对外仍下发（`apps/api/audit_center.py:414`、`:459`、`:541`、`:567`）。**建议负责人一次性统一口径** → `WAITING_OWNER` |
| **建议保留期限**（`WAITING_OWNER`） | **建议 10 年**，与仓库内已裁定值完全一致（`apps/api/audit_governance.py:49`）。本项**不新增**任何新数字，只是把已有裁定与 `audit_center.RETENTION_NOTE` 的冲突提请确认 |
| **起算时间** | `AuditEvent.created_at`（`apps/core/models.py:123`，`auto_now_add`）；`ApprovalRecord.created_at`（`apps/billing/models.py:72`），审批完成以 `approved_at`(:70) 为辅证；`ExportCredential.created_at`（`apps/core/models.py:937`） |
| **到期处理方式** | **代码层不可删**：`AuditEvent.save()` 对已存在实例抛错（`apps/core/models.py:144-147`）、`delete()` 抛错（`:149-150`）；`ApprovalRecord` 审批事实不可改、不可删（`apps/billing/models.py:108-140`）；`ExportCredential` 只追加、过期即失效不回收（`apps/core/models.py:903-907`）。<br>数据库层兜底：`apps/core/sql/audit_immutability.sql`（文件已存在，已 `Test-Path` 核实）+ 触发器安装/核验命令 `apps/core/management/commands/install_mysql_append_only_guards.py`、`check_mysql_append_only_guards.py`。本机 MySQL 账号无 SUPER 且开 binary log → 建触发器报 1419（`PROD-01至08与M5-4依赖状态表_2026-11-26.md:37`、`:76`）→ 触发器层验证 `PRODUCTION_PENDING`。<br>→ 到期销毁须走独立审批 + 只读备份（`apps/api/audit_governance.py:52`）；**仓库内未发现到期销毁执行器 → 未核实** |
| **Legal Hold 影响** | **无影响**——审计数据永不在清理范围（`apps/guests/retention.py:7-9`）。反向约束：保全期间**禁止**对审计/审批/导出凭证做任何销毁或归档 |
| **责任岗位** | 审计/合规：`core.audit_view` 权限（`apps/api/audit_governance.py:60`）；导出用途说明强制 8–240 字符（`:57-62`）；导出凭证 TTL 15 分钟（`:47`），每次创建与下载均写审计（`apps/core/models.py:905`）。**法律责任人：待负责人指定 → `WAITING_OWNER`** |
| **删除或匿名化执行方式** | **不执行删除、不执行匿名化**。到期销毁路径**当前无实现**（未核实） |
| **备份数据处理规则** | 审计库**不得**随普通业务备份的生命周期缩短而提前销毁；介质保留期 ≥ 审计数据保留期。`logs/audit.log` 文件日志与 `core_auditevent` **是两条独立载体**，前者按轮转策略处理（见 §2.5），后者按 10 年口径——**不可混为一谈**（`apps/api/audit_governance.py:226-229` 将 `logs/audit.log` 与 `docs/acceptance/evidence/` 并列为 evidence_paths，属指针而非留存策略） |

---

### 2.5 系统日志（System Logs）

| 字段 | 内容 |
|---|---|
| **数据范围** | ① `core_ratelimitcounter` — **限流计数器**（独立于 AuditEvent，`apps/core/models.py:565-597`）：`key_hash`(576)、`action`(577)、`window_start`(578)、`count`(579)、`expires_at`(580)。动作与窗口现取：`auth.login` 5 次/300 秒（`apps/core/rate_limit.py:29`）、`public.reservation_create` 10 次/600 秒 + 手机号 3 次/600 秒（`:30`）、`public.reservation_query`（`:31`）、`public.reservation_cancel`（`:32`）、`public.payment_intent`（`:33`）、`public.invitation_activate` 5 次/900 秒（`:34`）、`identity.invitation_manage` 30 次/600 秒（`:37`）、`pms.notification_refresh`（`:39`）、`identity.pwd_reauth` 5 次/300 秒 + 20 次/300 秒（`:46`）<br>② `core_outboxevent` — 事务性发件箱（`apps/core/models.py:954-994`）：`event_type`/`aggregate_type`/`aggregate_id`/`payload`(988-991)、`status`(992-994)、状态机含 `DEAD` 死信（`:973`、`:986`）<br>③ `core_notification`（`apps/core/models.py:600`）、`core_notificationread`(732)、`core_workitem`(762)、`core_businessdate`(265)、`core_nightauditrun`(426)<br>④ **文件日志**：`LOG_DIR = BASE_DIR / "logs"`（`hotel/settings.py:257`）；`audit.log`（`:269`）、`request.log`（`:277`），均为 `RotatingFileHandler`，`maxBytes=1024*1024`、`backupCount=5`（`:270-271`）；logger 绑定 `hotel.audit`/`django.request`/`hotel.api`/`hotel.core`/`hotel.billing`（`:284-291`） |
| **业务用途** | 反滥用与暴力破解防护（登录失败计数等）、异步投递可靠投递、站内通知与运营待办提示、夜审批次与营业日。⚠ `core_notification` 明确**不是**审计表：「生成/已读不落 AuditEvent，也不允许反向推导业务状态」（`apps/core/models.py:603-605`） |
| **法律或合同依据** | 依据类型：**正当业务目的**（LEGITIMATE_INTEREST）。具体期限仓库内无现取依据记录 → 未核实。注意：限流计数器**不得**充当审计记录（`apps/core/models.py:566`：「AuditEvent 不得充当计数器」） |
| **建议保留期限**（`WAITING_OWNER`） | 限流计数：**建议 = 窗口 TTL + 缓冲 7 天**（TTL 本身已在代码内固化：300/600/900 秒，`apps/core/rate_limit.py:28-47`）；超期无证明价值即删。<br>Outbox 事件：**建议 = 投递成功/死信处置完成后 90 天**（`DEAD` 死信须人工排查重放，`:973`，故不早于人工处置周期）。<br>站内通知/待办：**建议 = 业务事项闭环后 1 年**（提示性质，非业务状态权威，`:603-605`）。<br>文件日志：**建议 = 6 个月**（现状为 1MB×5 轮转，容量口径而非期限口径，`hotel/settings.py:270-271`）——建议负责人确认是否需要改配置 |
| **起算时间** | 限流：`expires_at` / `window_start`（`apps/core/models.py:578-580`）；Outbox：`created_at`；通知：`created_at`；文件日志：文件轮转生成时间 |
| **到期处理方式** | 限流计数：**TTL 物理删除**——`cleanup_expired(now)` 删除 `expires_at < now` 的行（`apps/core/rate_limit.py:175-179`）。⚠ **全仓未发现该函数的生产调用方**（grep 仅命中定义处）→ 计数行只增不减，属运维缺口；是否已挂外部 cron/crontab **未核实**。<br>Outbox：`DEAD` 死信人工排查重放（`apps/core/models.py:973`），不自动删除。<br>通知/待办：随 `resolved_at`/读标记沉没，无清理实现 → 未核实。<br>文件日志：**仅轮转，无按期限销毁**（`hotel/settings.py:266-283`）→ 缺口 |
| **Legal Hold 影响** | **无阻断点**——系统日志不含待保全主体，且不在清理路径内。反向提示：`core_outboxevent.payload`(991) 与 `core_notification` 可能携带业务标识（含 PII 片段），Legal Hold 期间应限制其可读范围（建议项 → 待裁定） |
| **责任岗位** | 运维/平台（TTL 参数、轮转与外部调度）；开发（清理函数与命令的可运维性）。限流动作参数集中于 `apps/core/rate_limit.py:28-47`，可由 `settings.PUBLIC_RATE_LIMITS` 覆盖（`:66-67`） |
| **删除或匿名化执行方式** | 限流计数：**物理删除**（`cleanup_expired`，`apps/core/rate_limit.py:175-179`），可挂定时任务。Outbox/通知：**无清理实现**。文件日志：轮转由 `RotatingFileHandler` 自动完成（`hotel/settings.py:266-283`），**按期限销毁需运维配置**（未核实是否有外部归档/清理脚本） |
| **备份数据处理规则** | 系统日志**不进入生产备份主流程**（无业务事实价值）；如已随库备份，则按介质统一到期销毁。文件日志目录 `logs/`（`hotel/settings.py:257-258`）通常不随数据库备份——**未核实**是否有独立日志归档策略 |

---

### 2.6 临时数据（Temporary）

| 字段 | 内容 |
|---|---|
| **数据范围** | ① `core_idempotencyrecord` — 通用幂等记录（`apps/core/models.py:467-521`）：`key`(495)、`method`/`path`(496-497)、`request_hash`(498)、`response_status`/`response_body`(499-500)、`status`(501-506)、`expires_at`(507)<br>② `core_exportcredential` — 一次性下载令牌（`apps/core/models.py:896`）：`token`(923)、`expires_at`(936)，TTL **15 分钟**（`apps/api/audit_governance.py:47`）<br>③ `reservations_bookingsaccesstoken` — 订单访问令牌（`apps/reservations/models.py:714-779`）：`token_hash`(725)、`expires_at`(726)、`revoked_at`(727)，默认有效期 = 离店日 + 30 天（`:746`），明文仅展示一次、库中只存 SHA-256（`:717-718`、`:744-745`）<br>④ `reservations_inventoryhold` — 库存冻结（`apps/reservations/models.py:542`）：`expires_at`(583)、`released_at`(585)；`reservations_pendingbookinggate`(831)<br>⑤ `django_session` — Django 会话表（`django.contrib.sessions` 已注册，`hotel/settings.py:55`）；会话 Cookie 按路径隔离（`apps/identity/session_middleware.py:207-323`）<br>⑥ `django.contrib.admin.LogEntry`（Django 内置，随 `django.contrib.admin` 注册，`hotel/settings.py:57`）——本轮**未逐字段核实** |
| **业务用途** | 幂等去重（重放返回原响应、同 key 异 body 报冲突，`apps/core/models.py:468-472`）、一次性导出下载授权、订单查询/取消凭据、库存临时占用与过期释放、登录会话 |
| **法律或合同依据** | 依据类型：**正当业务目的**（技术性中间数据，非独立法定义务）。订单访问令牌的业务依据为「手机号不得单独作为订单访问凭据」（`apps/reservations/models.py:715`） |
| **建议保留期限**（`WAITING_OWNER`） | 幂等记录：**建议 = `expires_at` + 1 天缓冲**（仓库内既有默认 `--older-than-days 1`，`apps/core/management/commands/cleanup_idempotency_records.py:22-23`）。<br>导出凭证：**15 分钟**（已固化，`apps/api/audit_governance.py:47`），过期即拒绝下载（`:204-206`）。<br>订单访问令牌：**离店日 + 30 天**（已固化，`apps/reservations/models.py:746`），撤销即刻失效（`:769-770`、`:775-779`）。<br>库存冻结：**随订单 expires_at 或释放即止**（`apps/reservations/models.py:583`）。<br>会话：**建议随浏览器关闭或框架默认过期即止**（`hotel/settings.py` 未设 `SESSION_COOKIE_AGE`/`SESSION_ENGINE` 覆盖 → 依赖 Django 默认值，**未核实**具体天数） |
| **起算时间** | 幂等记录：`created_at` / `expires_at`（`apps/core/models.py:507`）；导出凭证：`created_at`（`:937`）；订单令牌：`created_at`（`apps/reservations/models.py:728`）；库存冻结：`created_at`（`:584`）；会话：Django 框架 `expire_date` |
| **到期处理方式** | 幂等记录：**已有受控清理**——命令默认 **dry-run 只统计不删**，`--apply` 才删除；`expires_at IS NULL` 的记录**明确跳过不删**（需人工复核），见 `apps/core/management/commands/cleanup_idempotency_records.py:1-7`、`:31-57` 与 `apps/core/services.py:39-48`（`candidates = qs.filter(expires_at__lt=cutoff)`；`null_expiry_count` 单列）。<br>导出凭证：过期即失效（`apps/api/audit_governance.py:204-206`），不回收改期（`apps/core/models.py:906-907`）。<br>订单令牌：过期/撤销即失效（`apps/reservations/models.py:769-770`）。<br>库存冻结：`expire_reservation_holds` 命令（`apps/reservations/management/commands/expire_reservation_holds.py`）。<br>会话：Django 框架自动过期 |
| **Legal Hold 影响** | **无阻断点**（临时数据不含待保全主体）。⚠ 但 `core_idempotencyrecord.response_body`(500) 可能缓存含 PII 的响应体，`core_exportcredential.params`/`columns`(924-925) 可能记录查询口径 → **Legal Hold 期间建议冻结相关导出凭证与幂等记录读取**（建议项 → 待裁定，本轮不实施） |
| **责任岗位** | 平台/运维（TTL 与清理命令运维）；开发（清理命令 dry-run 复核与回执）。幂等记录清理由 `cleanup_idempotency_records` 承担，删除动作留审计摘要（`apps/core/services.py:24-26`） |
| **删除或匿名化执行方式** | **物理删除**（不匿名化、不留行）：幂等记录走 `cleanup_idempotency_records --apply`（分批删除，`apps/core/services.py:50-57`）；限流计数走 `cleanup_expired`（`apps/core/rate_limit.py:175-179`）。导出凭证与订单令牌为**逻辑失效**（不删行）。会话由框架处理 |
| **备份数据处理规则** | 临时数据**不进长期备份**；如已随库备份，恢复后由 TTL 再清理（幂等记录可重跑 `--apply` 幂等；限流计数可重跑 `cleanup_expired`）。备份介质销毁即视为临时数据销毁，**不得**以此为由延长介质保留期 |

---

### 2.7 Legal Hold 阻断能力现取现核结论（含 4 项缺口）

#### 2.7.1 阻断链路（逐段取证）

| 环节 | 现取事实 | 出处 |
|---|---|---|
| 唯一到期清理实现 | `plan_retention_cleanup(...)` —— 按策略算候选 → 过滤 LegalHold 与业务关联 → dry-run → 审批 → 受控匿名化 | `apps/guests/retention.py:126-305` |
| 唯一非测试调用方 | `apps/api/privacy_center.py:73`（dry-run）与 `:194`（正式执行），经 `POST /api/v1/privacy/retention/plans[/<id>/approve|/execute]` | `apps/api/privacy_center.py:52-90`、`:93-161`、`:164-213`；`apps/api/urls.py:342-356`；`hotel/urls.py:60` |
| 冻结集合求值 | `_active_legal_hold_guest_ids()`：`property` + `hold_status=ACTIVE` + `target_type="GUEST"` → `target_id` 集合 | `apps/guests/retention.py:69-77` |
| 求值时机 | 在 `plan_retention_cleanup` **函数入口第 143 行**执行 → dry-run 与正式执行**各自重算** | `apps/guests/retention.py:143` |
| 阻断判定 | 候选循环内 `if str(guest.pk) in hold_ids: skipped += 1; continue` —— 计入 **skipped 而非 failed**，不报错 | `apps/guests/retention.py:162-165` |
| 幂等与留痕 | 冻结创建/解除各写 `AuditEvent`（`privacy.legal_hold_created` / `privacy.legal_hold_released`） | `apps/guests/retention.py:380-387`、`:399-406` |
| 冻结记录不可篡改 | 冻结事实（对象/原因/申请人）变更抛 `ValidationError`；已解除不可再变更；`delete()` 抛 `RuntimeError` | `apps/guests/models.py:654-685` |

**结论**：在 `0fa7086` 基线上，**Legal Hold 能阻止到期清理路径**——ACTIVE 冻结的 Guest 主档不会进入匿名化，且**审批之后才下达的冻结同样阻断**（正式执行时重新求值）。`PASS_WITH_RESERVATIONS`（下述 4 项缺口）。

#### 2.7.2 四项缺口（只记录 + 建议，**本轮不实施**）

| # | 缺口 | 现取事实 | 影响 | 修复建议（不实施） |
|---|---|---|---|---|
| **G-1** | **Legal Hold 无产品化施加入口** | `request_legal_hold`/`confirm_legal_hold`/`release_legal_hold` 定义于 `apps/guests/retention.py:349`/`:367`/`:391`；全仓调用点**仅测试** `apps/guests/test_retention.py:219`；`privacy.legal_hold` / `privacy.legal_hold_release` **未注册进审批动作注册表**（`apps/billing/approval_registry.py` 仅注册 `billing.reversal`/`billing.refund`/`night_audit.reopen`/`privacy.export_staff`/`privacy.retention_execute`，见 `:168`、`:226`、`:254`、`:267`、`:284`） | **阻断能力成立，但「施加冻结」无产品路径**：真实诉讼/调查发生时，若无人手工以脚本创建 ACTIVE 冻结记录，清理仍会按期执行 → Legal Hold 名存实亡 | ① 在 `approval_registry` 注册 `privacy.legal_hold` / `privacy.legal_hold_release`（含 request/approve 权限与实体校验）；② 审批通过钩子调用 `confirm_legal_hold`；③ 补 API 端点（申请/查看/解除）与权限码 |
| **G-2** | **Legal Hold 只覆盖 Guest 主档，其它身份类无阻断点** | 阻断过滤硬编码 `target_type="GUEST"`（`apps/guests/retention.py:75`）；而 `RetentionPolicy.DataCategory` 声明了三类：`GUEST_PROFILE` / `GUEST_DOCUMENT` / `MARKETING_CONSENT`（`apps/guests/models.py:490-493`），清理实现**只处理 Guest 主档**（`apps/guests/retention.py:96-123`、`:308-346`）；证件类**完全没有到期清理实现**（`retention_until` 全仓仅在写入侧 `apps/guests/services.py:666-670` 出现，无任何据以清理的路径） | ① 证件（`GUEST_DOCUMENT`）保留期即使由负责人配置 `retention_days` 也**不产生任何清理动作**；② 会员/员工/旧表**无冻结阻断点**；③ 冻结粒度仅到主档，无法单独冻结某一证件或某一笔财务事实 | ① 将 `target_type` 扩展为枚举并逐类实现冻结过滤；② 为 `GUEST_DOCUMENT` 补清理实现（抹除 `doc_no_ciphertext`、保留 `doc_no_hash`）并把冻结过滤接入；③ 清理函数对 `data_category` 分派 |
| **G-3** | **逐条处理循环内不复查冻结** | 冻结集合在函数入口一次性求值（`apps/guests/retention.py:143`），逐条处理循环（`:271-278`）内不再检查 | 执行批次进行中新增的冻结，对**已在途**的该条不生效（批次内部分对象被清理） | 将 ACTIVE 冻结检查下沉到 `anonymize_guest()` 内（与 G-1 同处修复），使冻结判定与不可逆动作原子绑定 |
| **G-4** | **冻结状态与保留策略的写入无冻结校验** | `RetentionPolicy.save()` 只维护 `enabled_policy_key`，无「该类别是否有 ACTIVE 冻结」校验（`apps/guests/models.py:559-569`）；冻结解除 `release_legal_hold` 也不校验该类别是否存在待执行清理 | 冻结期间仍可**新增/改小**该物业该类别的 `retention_days` 或改 `enabled`，导致冻结解除后立即触发批量清理 | 在策略写入路径增加「该类别存在 ACTIVE 冻结时禁止缩短 `retention_days`」的校验（待负责人先定清策略） |

> **补充事实（非缺口）**：全仓**不存在**「到期即自动清理」的触发器——`apps/*/management/commands/` 实测 **33 个**命令模块（不含 `__init__.py`）中**无保留期清理命令**（含 `expire_pending_refunds`、`expire_memberships`、`expire_invitations`、`expire_reservation_holds`、`cleanup_idempotency_records` 等，均与保留期无关）。因此：**保留期限未定 → 不会发生生产数据被自动删除**（C-5 成立）；风险方向是「到期数据长期不删」而非「误删」。

---

## 三、证据索引

> 取证方式：`Read`（绝对路径）+ 限定 `grep`/`rg` + `git rev-parse --short HEAD`；未运行任何写库命令。

| # | 结论 | 证据（文件路径:行号 / 命令 + 结果） |
|---|---|---|
| E-1 | 代码基线 | `git rev-parse --short HEAD` → `0fa7086` |
| E-2 | 六类模型类名与表名 | `rg -n "^class \w+\((models\.Model\|.*models\.Model)\)" apps/*/models.py` → **79 个**模型类，逐文件：guests **9**（:9/132/243/313/358/445/487/579/691）、reservations **13**（:12/156/264/313/383/418/498/542/626/674/714/782/831）、identity **8**（:11/68/162/204/256/400/555/594）、inventory **11**（:7/51/144/191/262/335/436/532/574/619/711）、core **14**（:11/72/191/265/353/426/467/524/565/600/732/762/896/954）、billing **24**（:29/146/235/279/439/654/1068/1289/1485/1580/1629/1648/1754/1891/2025/2201/2286/2347/2379/2441/2599/2907/2983/3101） |
| E-3 | 表名口径 | `rg -n "db_table" apps/ --glob "*.py"` → 仅 `apps/billing/models.py:2672`、`:2693`、`:2716`、`:2761`、`:2809`、`:2859`、`:2904` 共 7 处显式覆盖；`rg -n "db_table" hotelapp/models.py` → `accommodation`:74、`client`:94、`food`:118、`order`:146、`park`:168、`finance_record`:200、`night_audit`:225、`folio`:255、`room`:284、`staff`:322、`vip`:346 |
| E-4 | 保留策略模型 | `apps/guests/models.py:487`（RetentionPolicy）、`:490-493`（DataCategory 三类）、`:495-498`（RetentionBasis 三值）、`:509-511`（retention_days）、`:515-517`（basis_note）、`:519-527`（enabled_policy_key）、`:554-556`（ck_retention_days_positive）、`:571-573`（clean 校验 >0） |
| E-5 | Legal Hold 模型 | `apps/guests/models.py:579`（LegalHold）、`:587-589`（HoldStatus）、`:597-598`（target_type/target_id）、`:620-621`（approval_reference）、`:643-652`（两条 CheckConstraint）、`:654-682`（事实不可改）、`:684-685`（delete 抛错） |
| E-6 | 清理执行记录 | `apps/guests/models.py:691`（RetentionExecution）、`:699-704`（ExecutionStatus 五态）、`:727-742`（as_of / candidate_count / processed_count / skipped_count / failure_count / failure_detail）、`:769-771`（正式执行必须带审批引用） |
| E-7 | 保留期执行服务 | `apps/guests/retention.py:1-19`（不变量）、`:34-36`（动作码）、`:39`（匿名化标记）、`:42-51`（未结束预订/退款状态集）、`:54`（MAX_CANDIDATES=500）、`:69-77`（冻结集合）、`:80-93`（未结业务判定）、`:96-123`（候选集与起算）、`:126-305`（主流程）、`:143`（冻结重算）、`:162-165`（跳过）、`:171-194`（dry-run）、`:196-260`（审批门与 SoD）、`:271-278`（逐条循环）、`:308-346`（匿名化）、`:349-407`（冻结申请/确认/解除） |
| E-8 | 保留期 API 与路由 | `apps/api/privacy_center.py:1-12`（含 `PRODUCTION_PENDING` 说明 :10-11）、`:27-42`（权限校验）、`:52-90`、`:93-161`、`:164-213`；`apps/api/urls.py:342-356`；`hotel/urls.py:60`（`api/v1/` 前缀） |
| E-9 | 审批动作注册表 | `apps/billing/approval_registry.py:198-202`（保留期实体校验）、`:282-291`（`privacy.retention_execute` 全规格）；`rg -n "action_code=\"[a-z_.]+\""` → 5 个已注册动作码（`:168`/`:226`/`:254`/`:267`/`:284`），**未含** `privacy.legal_hold*` |
| E-10 | 保留期专项权限 | `apps/identity/registry.py:511-538`（`guests.retention_request` / `retention_approve` / `retention_execute`，含岗位说明） |
| E-11 | 审计不可变 | `apps/core/models.py:72`（AuditEvent）、`:105-110`（before/after_json）、`:144-150`（禁改禁删）、`:152-188`（record 入口）；`apps/core/sql/audit_immutability.sql`（`Test-Path` → True）；`apps/core/management/commands/install_mysql_append_only_guards.py`、`check_mysql_append_only_guards.py` |
| E-12 | 审批不可变 | `apps/billing/models.py:29`、`:32`、`:85-140`（事实不可改 + 审批人≠申请人 :133-134 + delete 抛错 :139-140） |
| E-13 | 账本不可变 | `apps/billing/models.py:265-276`（QuerySet 守卫）、`:279`、`:330-335`（凭证号）、`:412-417`（冲销权限）、`:422-436`（save/delete 守卫） |
| E-14 | 留存年限既有裁定与冲突 | `apps/api/audit_governance.py:47`（导出凭证 TTL 15 分钟）、`:48-53`（`AUDIT_RETENTION_YEARS = 10` + 裁定原文）、`:198-206`（凭证过期校验）、`:209-232`（留存状态端点）；`apps/api/audit_center.py:40-45`（`RETENTION_NOTE` 冲突口径）、`:414`/`:459`/`:541`/`:567`（对外下发点） |
| E-15 | 限流与清理 | `apps/core/models.py:565-597`（RateLimitCounter）；`apps/core/rate_limit.py:27-47`（DEFAULT_LIMITS 全表）、`:175-179`（cleanup_expired）；`rg -n "cleanup_expired" --glob "*.py"` → 仅命中定义处，**无生产调用方** |
| E-16 | 幂等记录清理 | `apps/core/services.py:17-57`（dry-run 默认 True :18；候选 `expires_at__lt` :40；`null_expiry_count` :47；apply 分批删除 :50-57）；`apps/core/management/commands/cleanup_idempotency_records.py:1-7`、`:17-29`、`:31-57` |
| E-17 | 令牌与会话 | `apps/reservations/models.py:714-779`（订单令牌：明文一次 :717-718、默认离店+30 天 :746、失效判定 :769-770、撤销 :775-779）；`apps/identity/session_middleware.py:207-323`（SplitSessionMiddleware）；`hotel/settings.py:50-66`（INSTALLED_APPS，含 sessions :55 与 admin :57） |
| E-18 | 日志配置 | `hotel/settings.py:257-292`（LOG_DIR :257、audit.log :269、request.log :277、maxBytes/backupCount :270-271、loggers :284-291） |
| E-19 | PII 密钥 | `hotel/settings.py:36`（`DJANGO_PII_KEY`）、`:32-35`（密钥独立于 SECRET_KEY 的说明）、`:416`（生产校验 ≥32 字符） |
| E-20 | 管理命令全清单 | `Get-ChildItem apps -Recurse -Directory -Filter management` → **33 个**命令模块（不含 `__init__.py`），**无保留期清理命令** |
| E-21 | 发票缺失 | `rg -n "class Invoice\|db_table = 'invoice'\|Invoice" --glob "*.py"` → **零命中**；`rg -n "invoice" -i` → 仅 `hotel/settings.py:337`（菜单图标） |
| E-22 | 生产状态 | `docs/acceptance/PROD-01至08与M5-4依赖状态表_2026-11-26.md:32-39`（PROD-01~08 全为 `PRODUCTION_PENDING` 类标记）、`:47`（GATE-PROD 不得部分通过）、`:57-59`（M5-4-P1/P2/P3）、`:85`（禁止无依据删除） |
| E-23 | 历史记录及其失效点 | `docs/acceptance/OWNER-05-06裁定落实与执行边界记录_2026-11-25.md:23-26`（OWNER-05 暂缓、冻结）、`:38`（**已失效**的「无 API 端点调用」结论）、`:44-51`（建议期限留空的原口径）、`:55-57`（管理中心 UI 边界）、`:59-62`（PROD 冻结关联） |
| E-24 | 本轮未改代码 | 本轮工具调用仅 Read / grep / `git rev-parse` / `Test-Path`；未调用 `edit`/`write` 于任何 `.py`；未运行 `manage.py` 写操作；未跑 MySQL 全量测试 |

---

## 四、未完成与待裁定事项

### 4.1 待负责人确认项清单（全部 `WAITING_OWNER`）

| # | 待确认项 | 开发侧建议 | 确认口径 | 责任岗位 |
|---|---|---|---|---|
| W-1 | **六类数据保留期限最终值**（每类一个数字 + 起算点） | 见 §四.1 汇总表 | 逐行填「期限 + 依据类型 + 依据条文号（写入 `RetentionPolicy.basis_note`）+ 责任人」 | 负责人（数据保护责任人）+ 法务 |
| W-2 | **六类数据的法律责任人** | 开发侧不指定 | 每类指定姓名/岗位，写入责任人清单 | 负责人 |
| W-3 | 住客**证件**（`GUEST_DOCUMENT`）的实名登记留存期 | 建议 ≥ 财务凭证留存期（10 年口径） | 需法务提供具体条文号与年限 | 法务 + 负责人 |
| W-4 | **审计留存口径二选一**：`audit_governance.AUDIT_RETENTION_YEARS = 10`（已裁定）vs `audit_center.RETENTION_NOTE`「待确认、不设固定年限」 | 建议统一为 **10 年**并同步修订 `RETENTION_NOTE`（属代码改动，需另行授权） | 二选一并授权是否同步修订 | 负责人 |
| W-5 | **财务凭证留存期**（发票是否需要独立数据载体） | 建议 ≥ 10 年；发票载体建议复用 `LedgerEntry.reference` + `TaxLine`，或另立开发项 | 确认期限与是否建独立发票表 | 财务负责人 + 负责人 |
| W-6 | **是否解除生产批量匿名化/物理删除冻结** | 建议**暂不解除**，直至 W-1/W-2 完成 + PROD-03 备份恢复演练通过 + M5-4-P3 取得 L4 书面审批 | 书面解除或维持 | 负责人 |
| W-7 | **G-1 缺口修复授权**：Legal Hold 产品化施加入口（注册 `privacy.legal_hold` / `privacy.legal_hold_release` + 审批钩子 + API） | 建议**授权修复**（否则 Legal Hold 不可用，任务书「Legal Hold 必须能阻止到期清理路径」在业务上落空） | 授权 / 排期 / 拒绝 | 负责人 + 开发负责人 |
| W-8 | **G-2 缺口修复授权**：`GUEST_DOCUMENT` 清理实现 + `target_type` 枚举扩展 | 建议**授权修复** | 授权 / 排期 / 拒绝 | 负责人 + 开发负责人 |
| W-9 | **G-3 / G-4 修复授权**：冻结检查下沉到 `anonymize_guest`；冻结期间禁改 `retention_days` | 建议**授权修复**（G-3 与不可逆动作强相关） | 授权 / 排期 / 拒绝 | 负责人 + 开发负责人 |
| W-10 | 系统日志与文件日志的期限口径（限流/Outbox/通知/`logs/`） | 见 §2.5 建议 | 确认期限 + 是否改轮转配置 | 运维 + 负责人 |
| W-11 | 临时数据与会话的具体过期口径（`SESSION_COOKIE_AGE` 未在 settings 覆盖） | 建议由运维显式设定 | 确认天数并落到 settings | 运维 |

### 4.2 六类保留期限建议汇总表（`WAITING_OWNER`，未获批）

| 类别 | 主表（真实表名） | 建议保留期限 | 依据类型 | 起算点 | 到期处理 | Legal Hold 阻断 | 状态 |
|---|---|---|---|---|---|---|---|
| **身份信息** | `guests_guest`、`guests_guestdocument`、`guests_guestmembership`、`identity_staffprofile`、旧 `client`/`staff` | 主档 **10 年**；证件 **≥10 年**；员工 **10 年**；旧表随 M5-4 | 正当业务目的 / 法律法规要求 / 合同义务 | 末次离店日 / 证件失效日 / 离职日 | 主档**不可逆匿名化**（保留行）；证件**待实现**；会员置 EXPIRED；旧表不清理 | **仅主档被阻断**（G-2） | `WAITING_OWNER` |
| **业务数据** | `reservations_reservation`、`reservations_stay`、`inventory_room`、`billing_folio` | **10 年** | 合同义务 + 正当业务目的 | 退房日 / `actual_check_out` | **不清理**，到期只读冷存 | 无直接阻断（业务侧本就冻结优先） | `WAITING_OWNER` |
| **财务数据** | `billing_ledgerentry`、`billing_payment`、`billing_refund`、`billing_folio` | **≥10 年**（沿用已裁定口径） | 法律法规要求 | `business_date` / `paid_at` / `executed_at` | **代码层禁止删除/修改**；到期走独立审批 + 只读备份 | 无影响（天然不在清理范围） | `WAITING_OWNER` |
| **审计数据** | `core_auditevent`、`billing_approvalrecord`、`core_exportcredential` | **10 年**（已裁定，与 W-4 口径统一） | 法律法规要求 | `created_at` | **不可删**（模型 + DB 触发器）；到期走独立审批 | 无影响（天然不在清理范围） | `WAITING_OWNER` |
| **系统日志** | `core_ratelimitcounter`、`core_outboxevent`、`core_notification`、文件 `logs/audit.log`/`logs/request.log` | 限流 **TTL+7 天**；Outbox **90 天**；通知 **1 年**；文件日志 **6 个月** | 正当业务目的 | `expires_at` / `created_at` | 限流 TTL 物理删；死信人工处置；文件仅轮转（**无按期限销毁**） | 无阻断点 | `WAITING_OWNER` |
| **临时数据** | `core_idempotencyrecord`、`core_exportcredential`、`reservations_bookingsaccesstoken`、`django_session` | 幂等 **expires_at+1 天**；导出凭证 **15 分钟**；订单令牌 **离店+30 天**；会话 **框架默认** | 正当业务目的 | `created_at` / `expires_at` | 幂等记录受控命令清理（默认 dry-run）；其余逻辑失效 | 无阻断点（建议冻结期间限制读取） | `WAITING_OWNER` |

### 4.3 冻结边界声明

1. **不因保留期限未定而删除生产数据**：本轮未执行任何数据删除、匿名化或修改动作（E-24）；全仓亦无「到期即自动清理」触发器（E-20、C-5），故保留期限未定在技术上亦不会导致生产数据被自动删除。
2. **真实生产批量匿名化与物理删除继续冻结** —— 状态 `PRODUCTION_PENDING`。依据：`apps/api/privacy_center.py:10-11`；`docs/acceptance/OWNER-05-06裁定落实与执行边界记录_2026-11-25.md:23-26`；`docs/acceptance/PROD-01至08与M5-4依赖状态表_2026-11-26.md:85`。
3. **解除冻结的前置条件（四项全部满足方可解除）**：① 负责人确认六类保留期限与法律责任人（W-1/W-2）；② PROD-03 生产备份恢复演练通过（`PROD-01至08与M5-4依赖状态表_2026-11-26.md:34`）；③ M5-4-P3「L4 数据删除审批」书面批准（`:59`）；④ Legal Hold 施加入口可用（G-1 修复，否则清理期间无法施加保全）。
4. **不得据本报告直接归档、迁移、解 FK、RENAME 或 DROP**：与 M5-4 同批约束（`PROD-01至08与M5-4依赖状态表_2026-11-26.md:59`）一致。
5. **本报告不代替负责人拍板**：所有期限、责任人与修复授权均为 `WAITING_OWNER`；本轮**未实施**任何受其约束的业务变更（G-1~G-4 的修复建议仅登记，不改代码）。
6. **历史结论不覆盖**：`docs/acceptance/OWNER-05-06裁定落实与执行边界记录_2026-11-25.md:38` 的「生产清理无任何启用路径」结论在 `0fa7086` 已失效（E-8）；该文件本身**不修改**，其历史结论保留原样。

### 4.4 未核实项清单（复核方法）

| # | 未核实项 | 复核方法 |
|---|---|---|
| U-1 | 各类数据的**法条条文号**（个人信息保护 / 会计 / 税收征管 / 实名登记 / 劳动争议时效） | 仓库内无任何现取依据记录：`retention_days` 的模型定义**无 default 字面量**（`apps/guests/models.py:509-511`），`rg -n "retention_days\s*=\s*[0-9]"` 的命中**全部落在测试文件**（`apps/guests/test_retention.py:83/117/129`、`apps/guests/test_retention_gapfix.py:52`、`apps/api/test_privacy_center.py:62`、`apps/api/test_nse05_permission_matrix.py:323/828`），生产代码零年限字面量。条文号须由法律责任人提供并写入 `RetentionPolicy.basis_note`（`apps/guests/models.py:515-517`） |
| U-2 | **生产环境实际已生效的 `RetentionPolicy` 数据行**（各物业、各 `data_category` 的 `retention_days`/`enabled`） | 本轮不连生产库、不跑 MySQL 全量测试（共同说明红线 11）。复核：生产库执行 `SELECT property_id, data_category, retention_days, retention_basis, enabled, effective_from FROM guests_retentionpolicy;`，并核对 `enabled_policy_key` 唯一性（`apps/guests/models.py:519-527`） |
| U-3 | **生产数据规模与到期候选集实际量级**（`MAX_CANDIDATES=500` 分批，`apps/guests/retention.py:54`） | 生产库执行 dry-run 计划（`POST /api/v1/privacy/retention/plans`）读 `candidate_count`/`skipped_count`；需先取得 W-6 授权 |
| U-4 | `rate_limit.cleanup_expired`（`apps/core/rate_limit.py:175-179`）**是否已被外部调度**（cron / crontab / 运维脚本） | 全仓仅命中定义处，无内部调用方。复核：生产主机查 `crontab -l`、systemd timer、运维编排平台的定时任务清单 |
| U-5 | **文件日志 `logs/` 是否存在外部归档/按期限销毁机制** | 复核：生产主机查 `logs/` 目录的归档脚本与磁盘占用策略；仓库内仅见 `RotatingFileHandler`（`hotel/settings.py:266-283`） |
| U-6 | **会话有效期具体天数** | `hotel/settings.py` 未设 `SESSION_COOKIE_AGE` / `SESSION_ENGINE` 覆盖（`rg` 零命中）→ 依赖 Django 默认值。复核：`python manage.py shell -c "from django.conf import settings; print(settings.SESSION_COOKIE_AGE, settings.SESSION_ENGINE)"`（只读，不写库） |
| U-7 | `django.contrib.admin.LogEntry` 的实际字段与是否被用作员工操作日志 | Django 内置模型，本轮未逐字段核实。复核：后台「管理后台 → 操作日志」页面核对字段与行数 |
| U-8 | **审计留存口径冲突**中 `audit_center.RETENTION_NOTE` 的历史成因与对外口径影响面 | `apps/api/audit_center.py:40-45` 与 `apps/api/audit_governance.py:48-53` 并存；复核：确认是否需同步修订（属代码改动，需授权） |
| U-9 | **财务数据 / 审计数据的「到期销毁执行器」是否存在** | 仓库内未发现。复核：全局搜 `destroy|archive|purge|retire` 关键字并询问财务/DBA |
| U-10 | 生产备份介质的**保留周期与擦除策略** | PROD-03 未验收（`PROD-01至08与M5-4依赖状态表_2026-11-26.md:34`）。复核：DBA 提供备份介质台账与到期擦除 SOP |

---

## 五、下一步责任归属

| 序 | 事项 | 责任岗位 | 交付/动作 | 前置 | 建议时限 |
|---|---|---|---|---|---|
| 1 | 确认六类数据保留期限与法律责任人（W-1、W-2、W-3、W-5） | 负责人（数据保护责任人）+ 法务 | 逐行填写《§4.2 汇总表》并签署；在 `RetentionPolicy` 写入 `retention_days` / `retention_basis` / `basis_note` | 本报告 | 本轮任务书要求的下一个负责人决策窗口 |
| 2 | 统一审计留存口径（W-4） | 负责人 | 二选一裁定；如选 10 年，另行授权修订 `apps/api/audit_center.py:41-45` | 第 1 项 | 与第 1 项同批 |
| 3 | 授权修复 G-1（Legal Hold 产品化入口） | 负责人 + 开发负责人 | 审批动作注册 + 审批钩子 + API 端点 + 权限码 | 第 1 项 | 优先（阻塞清理可用性） |
| 4 | 授权修复 G-2 / G-3 / G-4 | 负责人 + 开发负责人 | `GUEST_DOCUMENT` 清理实现与冻结分派；冻结检查下沉到 `anonymize_guest`；冻结期间禁改 `retention_days` | 第 3 项 | 与第 3 项同批 |
| 5 | 维持或解除生产冻结（W-6） | 负责人 | 书面维持冻结，或在四项前置全部满足后书面解除 | 第 1、2、3 项 + PROD-03 + M5-4-P3 | 前置满足后 |
| 6 | 落档六类数据的**责任人清单**与本报告裁定结果 | 开发（文档维护） | 将负责人签署结果回填本报告并同步 `docs/acceptance/OWNER-05-06裁定落实与执行边界记录_2026-11-25.md` 的历史结论失效说明（**需主控会话统一处理，本次未修改该文件**） | 第 1、2 项 | 负责人裁定后 |
| 7 | 系统日志/临时数据/会话口径落地（W-10、W-11） | 运维 | 显式设定 `SESSION_COOKIE_AGE` / 限流清理调度 / 文件日志归档策略 | 第 1 项 | PMS3.0 开发准入前 |
| 8 | 生产侧取证（U-2、U-3、U-4、U-5、U-10） | DBA + 运维 + 财务 | 提供 `guests_retentionpolicy` 实际行数与启用情况、备份介质台账、外部调度清单 | 第 5 项解除冻结后 | 生产窗口 |

---

> **本报告纪律声明**：所有表名、字段名、行号、状态与数量均为 `main` HEAD `0fa7086` 基线现取现核；未修改任何 `.py`、未修改 `docs/acceptance/OWNER-05-06裁定落实与执行边界记录_2026-11-25.md`、未改动 `docs/文档索引.md` 与 `docs/共享规范/` 下任何主控会话维护的文件；未执行 `git` 写操作；未运行 MySQL 全量测试；未编造任何法条条文号、表名、行号或保留年限。
