# PMS 后台管理体系专项评审：第 12 节决策审核结论

> **文档状态：评审结论 / 待业务确认**  
> **评审对象：**《PMS 后台管理体系专项评审与优化规划 V1.0》第 12 节“需要审核确认的决策”  
> **评审日期：** 2026-11-03  
> **文档基线：** `b1e2776`  
> **本地代码基线：** `3b2240a`（V1.1 P0 模型定稿）  
> **评审范围：** 新旧数据权威、Django Admin 定位、旧模块退役原则，以及第 12 节七项决策的可执行前置条件  
> **结论性质：** 技术评审意见，不替代业务决策；本轮未修改业务代码、数据库或原规划文档

---

## 0. 结论速览

第 12 节的七个方向整体可以确认，但必须区分“目标态确认”和“现状已具备”。当前代码离目标态还有三个硬缺口：写入口未收口、审计未统一、权威服务未成型。

| # | 第 12 节决策 | 审核结论 | 一句话理由 |
|---|---|---|---|
| 1 | PMS2 为未来唯一业务写权威 | **有条件确认** | 方向正确，但“唯一”尚未落到服务级，旧写入口仍在 |
| 2 | Django Admin 收缩为平台维护/审计后台 | **确认定位，分阶段执行** | 身份与账务已只读，但客人、客房、预订 Admin 仍可写 |
| 3 | Client/Vip 历史映射至 Guest/GuestMembership | **有条件确认** | 旧字段不足、手机号唯一约束冲突，不能自动迁移 |
| 4 | 财务与夜审只认 PMS2 账本 | **有条件确认** | 账本模型已在，但 `NightAuditService` 不在，旧夜审仍可写 |
| 5 | 先处置公开 SQL 文件 | **确认，P0** | SQL 已被 Git 跟踪，只删当前文件不能解决历史泄露 |
| 6 | 餐饮/停车是否长期出范围 | **需业务确认** | 技术建议“出范围 + 保留第三方对接边界”，但旧代码仍在 |
| 7 | 单店优先、保留物业隔离 | **确认，附执行条件** | Property 已贯穿模型，但 Admin 和服务仍有默认物业捷径 |

**建议：** 把第 12 节从“是否确认”改写为“**原则确认 + 前置条件 + 验收口径**”。否则实施时会出现“文档说 Admin 只读，代码里 Guest、Room、Reservation 仍可写”的落差。

---

## 1. 评审基线与方法

### 1.1 基线说明

| 项目 | 结果 |
|---|---|
| 规划文档代码基线 | `b1e27768781a0734b6b9fc3d1d88da842800c924` |
| 本地仓库 HEAD | `3b2240ad9e5c67730221e16a4d9d518245d35da0` |
| 本地相对文档基线 | 领先一个提交，包含 V1.1 P0 模型 |
| 全量测试 | `131 tests OK` |
| 迁移漂移检查 | `No changes detected` |

本地代码领先于文档基线，主要增加了：

- `GuestPhone`、`GuestDocument`、`GuestMembership`、`MembershipLevelHistory`
- `ReservationGuest`、`StayGuest`、`InventoryHold`、`CancellationPolicy`、`DepositPolicy`
- `HousekeepingTask`、`MaintenanceTicket`、`RoomInspection`
- `FolioTransfer`、`AuditEvent`、`LegacyRecordLink`、`BusinessDate`、`NightAuditRun`

但多数新增模型目前只有 schema：没有对应服务、没有 Admin 入口、没有 API。**“模型已建”不等于“业务权威已建立”。**

### 1.2 评审方法

本次评审采用以下方式：

1. 通读第 12 节及前后相关章节，提取七项待确认决策。
2. 对照本地仓库的模型、服务、Admin、URL、菜单和测试。
3. 执行全量测试与迁移漂移检查，确认代码基线可用。
4. 只读检查 SQL 文件的表名与 INSERT 数量，不复制任何记录值。
5. 对每条决策给出“确认 / 有条件确认 / 需业务确认”的判断，并列出可执行前置条件。

### 1.3 三个硬缺口

#### 缺口一：写入口未收口

旧 `hotelapp` 写路由仍全部挂载在 `hotel/urls.py`，旧 Admin 仍可修改业务表，新 Admin 中 `Guest`、`Room`、`Reservation`、`Stay`、`DailyRate`、`RoomTypeInventory` 仍可写。

其中多个旧删除/冲账入口仍使用 GET 直接写库，例如：

- `hotelapp/views.py:397-403`：`delete_client` 使用 GET 直接 `.delete()`
- `hotelapp/views.py:976` 起：`delete_accomodation` 使用 GET 执行“冲账”
- `hotelapp/views.py:1257` 起：`room_status_change` 直接改旧房态

#### 缺口二：审计未统一

`AuditEvent` 目前只在以下位置实际写入：

- `apps/guests/services.py`：证件登记、证件揭示
- `apps/identity/services.py`：员工生命周期、角色授权

前台排房、入住、退房、续住、换房、No-Show、押金、退款仍主要写文件日志：

- `apps/reservations/front_desk_services.py:14`：`audit = logging.getLogger("hotel.audit")`
- `apps/reservations/lifecycle_services.py` 同样使用文件日志

因此“审计权威”尚未真正建立。

#### 缺口三：权威服务未成型

- 账务写入口分散在 `apps/billing/services.py`、`apps/reservations/front_desk_services.py`、`apps/reservations/lifecycle_services.py`。
- `BusinessDate`、`NightAuditRun` 模型已建，但没有 `NightAuditService`，旧 `nightaudit_run` 仍在使用。
- `GuestMembership`、`GuestPhone`、`HousekeepingTask`、`MaintenanceTicket`、`RoomInspection` 等没有对应服务和工作台。
- `InventoryHold`、`ReservationGuest`、`StayGuest`、`CancellationPolicy`、`DepositPolicy` 等没有 Admin 或服务入口。

---

## 2. 第 12 节七项决策逐条审核

### 2.1 决策 1：PMS2 为未来唯一业务写权威

**文档原意：** 旧 `hotelapp` 表仅保留历史只读与迁移映射，不再双写。

**审核结论：有条件确认。**

需要先把“唯一写权威”定义清楚，否则容易变成口号。建议定义为：

> 一个业务事实，只允许一个服务入口写，只落到一张事实表；其他模块只能调用该服务，不得直接 ORM 写入。

当前代码不符合这个定义的地方很具体：

| 位置 | 问题 |
|---|---|
| `apps/reservations/front_desk_services.py:289` | 直接创建 `Payment` |
| `apps/reservations/front_desk_services.py:301`、`:356` | 直接创建 `LedgerEntry` |
| `apps/reservations/lifecycle_services.py:354`、`:360` | 直接创建 `Payment`、`LedgerEntry` |
| `apps/reservations/lifecycle_services.py:400`、`:433` | 直接创建 `Refund`、`LedgerEntry` |
| `apps/reservations/services.py:146` | 直接创建 `Folio` |

这说明：**账务的“唯一写权威”目前不存在**，只是“账务 Admin 只读”。前台服务绕过 `BillingService` 直接写账本，后续退款、冲销、日结、对账都会分叉。

**前置条件：**

1. 建立 `BillingService`，统一 `post_entry / record_payment / request_refund / approve_refund / reverse_entry`。
2. 前台服务改为调用 `BillingService`，不允许直接 `Payment.objects.create`、`LedgerEntry.objects.create`。
3. 旧 `hotelapp` 写路由冻结，旧表运行时只读。
4. 迁移脚本单独开关/账号，不与业务运行混用。
5. 所有写操作带 `property`、操作人、原因，并写 `AuditEvent`。

**验收口径：**

- 旧表在业务运行中新增/修改记录数为 0（迁移脚本除外）。
- 任一业务事实只有一个服务入口能写。
- 账本只能追加，错账只能红字冲销，不能改删。

---

### 2.2 决策 2：Django Admin 收缩为平台维护/审计后台

**文档原意：** 酒店日常业务管理使用独立 PMS 后台工作台，所有写操作调用业务服务/API。

**审核结论：确认定位，但必须分阶段，不能一刀切。**

先澄清一个概念：

> Django Admin 可以作为“审计展示后台”，但审计权威不是 `django_admin_log`，而是 `AuditEvent`。

`django_admin_log` 只记录 Admin 内的变更，不记录 API、前台工作台、服务层写入。第 12 节如果写成“Admin 作为审计后台”，容易被误解为 Admin 日志就是审计事实，这一点需要修正。

当前 Admin 实际情况：

| App | 现状 |
|---|---|
| `apps.identity` | `StaffProfile`、`PropertyStaffMembership`、`StaffRoleAssignment` 已只读 |
| `apps.billing` | `Folio`、`LedgerEntry`、`Payment` 字段全只读；`Refund` 未注册 |
| `apps.guests` | `Guest` 可写，仅创建/更新时间只读 |
| `apps.inventory` | `Room`、`DailyRate`、`RoomTypeInventory` 可写；库存仅 `version/updated_at` 只读 |
| `apps.reservations` | `Reservation`、`Stay` 可写，仅少数字段只读 |
| `apps.core` | 只注册 `Property`；`AuditEvent`、`LegacyRecordLink`、`BusinessDate`、`NightAuditRun` 无入口 |
| `hotelapp` | 只禁止物理删除；`Staff/Client/Room/Vip/FinanceRecord/Folio` 仍可改；`NightAudit` 禁止修改 |

**分两批收紧：**

**第一批，立即收紧：**

- 账务：`Folio`、`LedgerEntry`、`Payment` 明确 `has_change_permission=False`；`Refund` 注册为只读。
- 身份：保持只读。
- 旧财务：`hotelapp.FinanceRecord/Folio` 改为只读；`NightAudit` 保持禁止修改。
- 审计：`AuditEvent`、`LegacyRecordLink`、`BusinessDate`、`NightAuditRun` 注册为只读，放入“审计与历史”菜单。

**第二批，等新入口覆盖后再收紧：**

- `Guest`：先补 `GuestService` 和客人编辑工作台，再关 Admin 写。
- `Room`：先补房态流转服务，再关 Admin 写；否则目前只能靠旧 `room_status` 路由改房态。
- `Reservation/Stay`：先冻结状态、金额、日期字段，再整体只读；否则改期、改量、纠正错误没有入口。
- `DailyRate/RoomTypeInventory`：先补价格/库存调整服务（带原因、审批、审计），再禁止手工改 `held/sold`。

执行顺序必须是：**新入口先覆盖，旧入口后关闭。**

---

### 2.3 决策 3：Client/Vip 历史映射至 Guest/GuestMembership

**文档原意：** 先候选匹配、人工复核，不自动合并。

**审核结论：有条件确认。**

代码依据：

- 旧 `Client` 的核心字段是姓名、身份证号；身份证号全局唯一。
- 旧 `Vip` 的核心字段是姓名、会员等级、联系方式；联系方式唯一。
- 新 `Guest` 要求 `property + full_name + phone`，并且有 `(property, phone_normalized)` 唯一约束（`apps/guests/models.py:35-40`）。
- 新 `GuestMembership` 需要 `guest` 外键，不是独立会员表。
- 新 `GuestDocument` 的证件号是密文 + HMAC 哈希，查重约束是 `(guest, doc_type, doc_no_hash)`，不是全局身份证唯一。
- `LegacyRecordLink` 已支持 `source_model/source_pk/target_model/target_pk`，可作为映射留痕。

因此，第 12 节“Client/Vip 历史映射至 Guest/GuestMembership”不能理解成字段复制。至少要补四件事：

1. **先决策手机号唯一策略。** `(property, phone_normalized)` 唯一约束与 `GuestPhone` 的“多号码、不以手机号为自然人主键”设计不一致，会阻塞一客多号、家庭共用号、旧 Vip 联系方式迁移。迁移前必须决定保留还是解除该约束。
2. **候选匹配规则。** 证件 hash 优先，其次手机号，其次姓名 + 其他辅助信息；不能仅凭姓名自动合并。
3. **人工复核队列。** `LegacyRecordLink` 的 `MATCH_CANDIDATE / CONFLICT / LINKED` 要用起来，不允许自动合并。
4. **旧 Client 身份证迁移。** 旧明文身份证必须先加密再写 `GuestDocument`，不能直接落明文；同时保留 `LegacyRecordLink` 映射。

**验收口径：**

- 每条旧 Client/Vip 都有明确状态：已链接、候选、冲突、忽略。
- 不存在“自动合并后无法回溯”的记录。
- 旧 `Client/Vip` 表冻结写，但保留只读查询和映射结果查看。

---

### 2.4 决策 4：财务与夜审只认 PMS2 账本

**文档原意：** 旧 `FinanceRecord/NightAudit/Folio` 只读归档，不再用于新业务报表。

**审核结论：有条件确认。**

代码依据：

- `LedgerEntry` 已设计为不可变账本：`ImmutableLedgerEntryQuerySet.delete/update` 直接抛错，错账用红字冲销。
- `Folio/LedgerEntry/Payment` Admin 字段全只读，但 `Refund` 未注册；`Folio` 也未显式关闭 `change` 权限。
- `BusinessDate`、`NightAuditRun` 模型已建，但没有 `NightAuditService`；旧 `nightaudit_run` 路由仍在 `hotel/urls.py`，旧视图仍可写旧 `NightAudit`。
- 旧 `FinanceRecord/Folio` 仍可通过旧页面和旧 Admin 修改。
- 前台服务直接写 `LedgerEntry`，尚未统一走 `BillingService`。

因此“只认 PMS2 账本”要分两步：

1. **先把账本写入口收口。** 所有房费、押金、收款、退款、冲销只走 `BillingService`；`Refund` 必须有申请、审批、审计。
2. **再切换报表和夜审。** 建立 `NightAuditService`，以 `BusinessDate` 为营业日边界，生成 `NightAuditRun`；旧 `NightAudit` 只读归档，不再作为新报表来源。

**前置条件：**

- 新旧账目对账规则先定：房费、押金、收款、退款、应收余额分别对账，不允许旧表和新表混算。
- 旧 `FinanceRecord` 中的非房费收入/支出要有承接方案，不能因为冻结旧财务页面而丢失记账能力。
- 历史 Folio 仍需可查，不能直接删旧页面。

---

### 2.5 决策 5：先处置公开 SQL 文件

**文档原意：** 核查真实数据；必要时私有化存储、清理 Git 历史并轮换凭据。

**审核结论：确认，P0。**

仓库依据：

| 文件 | INSERT 数量 | 涉及表类别 |
|---|---|---|
| `hotel.sql` | 105 | `auth_user`、`client`、`vip`、`staff`、`accommodation`、`room`、`food`、`order`、`park` 等 |
| `docs/db_backup_pre_pms2_20261030.sql` | 24 | `auth_user`、`client`、`vip`、`staff`、`accommodation`、`finance_record`、`folio`、`ledger_entry`、`night_audit` 等 |
| `docs/backup_table_audit_log…sql` | 0 | 无 INSERT |

两个主要文件都被 Git 跟踪，且历史提交中存在（如 `45e0fb5`、`6404b1c`）。因此：

- **只删除当前工作区文件不够**，Git 历史、fork、cache、镜像仍可能保留。
- 先由数据负责人核实是否为真实数据、是否含可用密码哈希/个人信息。
- 若确认敏感：迁移到私有受控存储，清理 Git 历史，轮换相关密码/密钥，检查登录记录。
- 仓库只保留脱敏 schema 或 sample fixture。

这一项不需要等业务确认，技术上应作为 P0 立即推进；但“是否真实敏感”必须由数据负责人签字，不能由开发凭字段名判断。

**注意：** 本评审不复制任何 SQL 记录值，只记录表名类别与 INSERT 数量。

---

### 2.6 决策 6：餐饮/停车是否长期出范围

**文档原意：** 若确认出范围，规划归档后删除旧页面/路由/权限/代码；否则明确未来对接接口与保留策略。

**审核结论：需业务确认；技术建议“确认出范围，但保留第三方对接边界”。**

代码依据：

- `hotelapp/views.py:47-50` 的 `MODULE_REGISTRY` 已把 `food`、`order`、`park` 关闭。
- `hotelapp/admin.py` 未注册 Food/Park/Order，直接访问 Admin 返回 404。
- 但模型、数据库表、URL、模板、权限项仍在；测试也仍在断言这些入口不可访问。

建议不要在“保留旧 CRUD”和“彻底删除”之间摇摆，而是明确三层：

1. **自建范围：** 餐饮/停车不做店内自建管理。
2. **集成边界：** 保留第三方 POS/停车系统的单号、金额、对账、手工补录接口。
3. **旧模块退役：** 旧 Food/Park/Order 按“冻结写 → 只读 → 归档 → 删除”处理，不恢复旧 CRUD。

如果业务确认“未来可能自建”，也要先冻结旧代码，未来用新模块重建，不能把旧 `hotelapp` 当未来基础。

---

### 2.7 决策 7：单店优先、保留物业隔离

**文档原意：** 当前先优化单店运营，但不破坏 `Property` 维度和未来多物业权限隔离。

**审核结论：确认，附执行条件。**

代码依据：

- `Property` 模型已建立，库存、预订、账务、客人、身份模型普遍带 `property` 外键。
- `hotel/settings.py:214-216` 明确当前单店运行，PMS2 菜单隐藏但 URL 保留。
- 但 `apps/identity/services.py` 存在 `_default_property()` 这类单店捷径；Admin 也没有按登录用户物业范围过滤 `get_queryset`。

因此“单店优先”不能变成“写死 DEFAULT”：

- 唯一约束必须带 `property`，不能出现全局唯一业务键。
- 所有服务入参必须显式带 `property`，`_default_property()` 只允许用于初始化/迁移，不允许扩散到业务写路径。
- Admin 的只读模型必须按用户任职物业过滤；超级管理员才可跨物业。
- 报表、导出、搜索同样按物业隔离，跨物业报表单独授权。

---

## 3. 重点一：新旧数据权威矩阵

### 3.1 服务级权威矩阵

| 业务事实 | 目标唯一写入口 | 当前实际写入口 | 先补什么 |
|---|---|---|---|
| 员工/任职/角色 | `EmployeeLifecycleService` | `apps/identity/services.py`；Admin 只读 | 保持；旧 `Staff` 冻结写 |
| 客人/电话/证件 | `GuestService` + `GuestDocumentService` | 只有证件服务；`Guest` 无服务，Admin 可写 | `GuestService`、去重/合并、手机号约束决策 |
| 会员/等级 | `MembershipService` | 模型有，无服务/Admin | 等级规则、等级历史、旧 Vip 映射 |
| 房型/房价/房间 | `InventoryService` / `RoomService` | 库存服务有；房态无服务；Admin 可写 | `RoomStatusService`、价格版本 |
| 库存占用 | `InventoryService` | `hold/confirm/release` 已有 | Admin 只读；库存调整服务 |
| 预订 | `ReservationService` | `apps/reservations/services.py` | Admin 只读；改期/改量服务 |
| 排房/入住/退房/续住/换房/No-Show | `FrontDeskService` / `LifecycleService` | 已有，但直接写账务、写文件日志 | 收敛账务写入口、统一 `AuditEvent` |
| 账单/分录/收款/退款 | `BillingService` | `billing/services.py` + 前台服务直写 | 收敛所有 `Payment/LedgerEntry/Refund` 写入口 |
| 营业日/夜审 | `NightAuditService` | 模型有，无服务；旧夜审仍活跃 | 建服务、切报表、旧夜审只读 |
| 房务/维修/检查 | `HousekeepingService` | 模型有，无服务/Admin | 建服务和工作台 |
| 审计 | `AuditEvent.record` | 仅客人证件/员工；前台写文件日志 | 全量接入前台、账务、Admin 高危操作 |
| 历史映射 | `LegacyMigrationService` | `LegacyRecordLink` 模型有，无服务 | 候选匹配、人工复核、对账工具 |

### 3.2 三条硬规则

1. **事实表只允许对应服务写。** 跨服务只能调用，不得直接 ORM 写入。
2. **旧表运行时只读。** 迁移脚本单独账号/开关，不能与业务运行混用。
3. **所有写操作带 `property + actor + reason + AuditEvent`。** 跨物业写入直接拒绝。

### 3.3 当前最危险的五个绕过点

| 绕过点 | 风险 |
|---|---|
| 前台服务直接创建 `Payment/LedgerEntry/Refund` | 绕过账务服务、退款审批、日结和对账 |
| `Guest`、`Room`、`Reservation`、`Stay` Admin 可写 | 绕过客人去重、房态流转、库存锁、状态机 |
| 旧 `delete_*` 使用 GET 直接写库 | CSRF/预取风险，且绕过新业务规则 |
| 旧 `FinanceRecord/Folio/NightAudit` 可写 | 新旧账本分叉，报表口径失真 |
| 旧 `Client/Vip` 与 `Guest` 并行存在 | 同一自然人两套档案，会员权益和入住历史无法统一 |

---

## 4. 重点二：Django Admin 定位与三级清单

### 4.1 定位

> Admin 是平台维护和只读审计后台，不是酒店日常业务工作台；审计事实以 `AuditEvent` 为准，不以 `django_admin_log` 代替。

### 4.2 三级清单

下表为**目标态清单**；标为 L1 但当前尚未注册的模型（如 `GuestPhone`、`GuestMembership`、`Refund`、`AuditEvent` 等），需要补只读注册。

| 级别 | 定位 | 模型 | 实现要求 |
|---|---|---|---|
| L1 只读 | 业务事实查询、审计查看 | `StaffProfile`、`PropertyStaffMembership`、`StaffRoleAssignment`；`Guest`、`GuestPhone`、`GuestDocument`、`GuestMembership`、`MembershipLevelHistory`；`Reservation`、`Stay`；`Folio`、`LedgerEntry`、`Payment`、`Refund`；`Room`、`RoomTypeInventory`；`AuditEvent`、`LegacyRecordLink`、`BusinessDate`、`NightAuditRun` | `has_add/change/delete = False`；PII 脱敏；按物业过滤 |
| L2 受控可写 | 平台主数据/配置 | `Property`、`RoomType`、`RatePlan`、`DailyRate`、`CancellationPolicy`、`DepositPolicy` | 仅平台管理员/超管；生效区间校验；写 `AuditEvent`；禁止删除 |
| L3 不开放写 | 内部过程/安全敏感 | `InventoryHold`、`FolioTransfer`、`StayRoomSegment`、`NoShowRecord`、`HousekeepingTask`、`MaintenanceTicket`、`RoomInspection`；`auth.User/Group` | 不在酒店业务菜单；只允许服务/运维脚本；超管应急需审批 |

### 4.3 当前必须先收紧的七处

1. `apps/guests/admin.py:11`：`GuestAdmin` 只读时间戳，需加 `has_add/change/delete=False`。
2. `apps/inventory/admin.py:13-18`：`RoomAdmin` 可改房态，需先补房态服务，再改只读。
3. `apps/inventory/admin.py:27-39`：`DailyRate`、`RoomTypeInventory` 可改价格/库存，需改为受控或只读。
4. `apps/reservations/admin.py:13-27`：`Reservation`、`Stay` 可改，需先冻结状态/金额/日期字段。
5. `apps/billing/admin.py`：`Folio/Ledger/Payment` 虽字段全只读，但建议显式 `has_change_permission=False`；`Refund` 注册只读。
6. `apps/core/admin.py`：只有 `Property`，需补 `AuditEvent`、`LegacyRecordLink`、`BusinessDate`、`NightAuditRun` 只读入口。
7. `hotelapp/admin.py`：目前只禁止删除，需把 `Staff/Client/Room/Vip/FinanceRecord/Folio` 改为只读或移入“历史只读”菜单。

另外，`hotelapp.NightAudit` 仅对个体对象关闭了修改权限，新增入口尚未关闭，需补 `has_add_permission=False`，避免在 Admin 中补造夜审记录。

### 4.4 Admin 加固清单

- 所有 L1 模型：`has_add_permission / has_change_permission / has_delete_permission` 全部返回 `False`。
- 所有 L1 模型：`readonly_fields` 覆盖全部字段，避免“字段没列出来但可编辑”。
- 带 `property` 的模型：重写 `get_queryset`，按当前用户任职物业过滤；超管除外。
- PII 字段：列表页不显示明文，搜索不直接匹配明文，导出需独立权限并写审计。
- L2 模型：`save_model` 写 `AuditEvent`，记录操作人、对象、变更前后值。
- 旧 `hotelapp` 模型：移入“历史数据（只读）”菜单，或直接取消注册。
- 菜单：移除“酒店业务模块”旧 8 项，改为“PMS 工作台”“平台维护”“审计与历史”三组。

---

## 5. 重点三：旧模块退役原则

### 5.1 两条总原则

1. **按业务域分批冻结，不按文件一刀切。**
2. **新入口覆盖是冻结的前置门禁，不能先关旧入口再补新入口。**

### 5.2 四阶段与门禁

| 阶段 | 动作 | 门禁 | 回滚 |
|---|---|---|---|
| G0 冻结写 | 菜单下线；旧写路由返回 410/403；旧 Admin 只读；数据库账号收回旧表写权限 | 新入口覆盖清单经业务签字 | 开关恢复 |
| G1 只读 | 旧页面只读；保留历史查询；旧数据核对 | 每个旧业务域都有只读查询入口 | 恢复写开关 |
| G2 归档 | 导出 CSV/JSON + 校验和；私有存储；恢复演练 | 财务/业务签字确认账实一致 | 从归档恢复 |
| G3 删除 | 删路由/模板/权限/代码/表 | 依赖扫描通过；至少一个完整营业周期只读；无新模型外键引用 | Git revert + 归档恢复 |

### 5.3 建议先冻结的旧写入口

**菜单：**

- `hotel/settings.py:224-231` 中“酒店业务模块”8 项：
  - 员工信息管理
  - 顾客管理
  - 会员信息管理
  - 客房信息管理
  - 客房订单管理
  - 财务流水
  - 夜审记录
  - 账单流水

**路由：**

- 员工：`index/insert/delete_staff/update_staff/search_staff/staff_toggle`
- 顾客：`show_client/insert_client/delete_client/update_client/search_client`
- 会员：`show_vip/insert_vip/delete_vip/update_vip/search_vip`
- 客房：`show_room/insert_room/delete_room/update_room/search_room`
- 客房订单：`show_accomodation/insert_accomodation/delete_accomodation/search_accomodation`
- 财务：`finance/finance_add`
- 夜审：`nightaudit/nightaudit_run`
- 旧入住：`checkin/legacy`、`checkin_confirm/checkin_in/cancel_booking/checkout/uncheckout/extend_stay/change_room/express_checkout/walkin_create/no_show_one`
- 房态：`room_status`
- 账单：`folio`
- 旧权限矩阵：`/permissions/`、`/permissions/save/` 仍由 `apps.identity.permission_admin` 按 `hotelapp` 权限项读写；旧模块退役前必须迁移到 PMS2 权限目录，不能只删页面，否则现有角色授权会失效。

**特别处理：**

- `delete_client`、`delete_accomodation` 等 GET 写操作，应优先改为 POST 或直接 410，不能再保留“GET 删除/冲账”。
- 旧写入口冻结后，不要让旧 URL 静默重定向到新服务写接口；这会造成“看起来旧入口还在写”的歧义。应明确返回 410，并在页面提示新入口。

### 5.4 永久保留、不得删除

- 所有 Django migrations。
- `AuditEvent`、`LedgerEntry`、`Payment`、`Refund`、`Folio` 历史数据。
- `LegacyRecordLink` 映射记录。
- `/api/v1/front-desk/legacy-reservations` 这类历史只读查询入口，直到迁移完成。
- 归档数据和恢复说明。

### 5.5 删除门禁清单

删除任何旧模块前，必须同时满足：

1. 全仓无 URL、菜单、模板、静态资源引用。
2. 无新模型外键引用旧表。
3. 旧数据已归档，校验和与恢复演练通过。
4. 至少一个完整营业周期只读，无业务投诉。
5. 财务、前台、客房、管理层签字确认。
6. 删除提交独立，不与新功能混在一起，确保可 revert。

---

## 6. 第 12 节建议改写稿

建议将第 12 节替换为以下条文：

1. **确认 PMS2 为唯一业务写权威。** 唯一性按“业务事实 → 唯一服务入口 → 唯一事实表”定义；旧 `hotelapp` 表只读并保留迁移映射，不双写；冻结旧写入口是实施第一步。
2. **确认 Django Admin 定位为平台维护与只读审计后台。** 日常业务写操作只走 PMS 工作台/API；高风险模型 Admin 只读；审计事实以 `AuditEvent` 为准，不以 `django_admin_log` 代替。
3. **确认 Client/Vip 迁移采用“候选匹配 + 人工复核 + LegacyRecordLink”。** 不自动合并；迁移前先解决 `uq_guest_property_phone` 与多号码/共享号策略冲突。
4. **确认财务与夜审只认 PMS2 账本。** 旧 `FinanceRecord/NightAudit/Folio` 冻结写、只读归档；新报表只读 PMS2；先补 `NightAuditService/BusinessDate` 再切换。
5. **确认 SQL 文件处置为 P0。** 已跟踪文件不能只删当前版本；需核查 Git 历史、私有化存储、轮换凭据，并保留脱敏 schema/fixture。
6. **确认餐饮/停车长期不在自建范围。** 保留第三方 POS/停车对接边界；旧模块按“冻结写 → 只读 → 归档 → 删除”退役，不恢复旧 CRUD。
7. **确认单店优先但 Property 维度不可写死。** 唯一约束、服务入参、Admin 查询、报表和导出都保留物业隔离；`_default_property()` 不得扩散到业务写路径。

---

## 7. 实施路线建议

### P0：先止血，避免继续分叉

| 任务 | 产出 | 退出条件 |
|---|---|---|
| SQL 文件核查与私有化 | 数据负责人结论、私有存储、凭据轮换记录 | 公开仓库不再含真实数据 |
| 冻结旧财务/夜审/员工/客人写入口 | 旧路由 410/403、旧 Admin 只读 | 旧表无新增业务写入 |
| Admin 第一批只读 | 账务、身份、审计、历史映射 | L1 模型写权限为 0 |
| 前台接入 `AuditEvent` | 排房、入住、退房、收款等写审计 | 前台关键写操作 100% 有审计事件 |
| 物业范围过滤 | Admin `get_queryset`、服务入参校验 | 跨物业访问为 0 |

### P1：建立新权威

| 任务 | 产出 | 退出条件 |
|---|---|---|
| `BillingService` 收口 | 统一收款、退款、冲销、记账 | 前台不再直接创建 `Payment/LedgerEntry/Refund` |
| `GuestService` | 客人建档、去重、合并、手机号策略 | 旧 Client/Vip 可映射且可回溯 |
| `RoomStatusService` | 房态流转、清洁/维修联动 | Admin 不再需要改房态 |
| `NightAuditService` | 营业日切换、夜审运行、报表 | 新报表只读 PMS2 |
| 旧业务域只读入口 | 历史客人、订单、账务、夜审查询 | 旧页面可下线而不丢历史 |

### P2：退役与清理

| 任务 | 产出 | 退出条件 |
|---|---|---|
| 餐饮/停车归档 | 数据导出、校验、归档 | 旧模块可删除 |
| 旧代码/模板/权限清理 | 删除旧路由、视图、模板、权限项 | 依赖扫描通过 |
| 静态资源清理 | 去重、改引用、删除 | 全仓引用检查通过 |
| 仓库只保留脱敏 schema/fixture | 脱敏样例 | 无真实数据 |

---

## 8. 验收口径

### 8.1 数据权威

- 旧表在业务运行中新增/修改记录数为 0（迁移脚本除外）。
- 任一业务事实只有一个服务入口能写。
- 账本只能追加，不能改删；错账有红字冲销。
- 新旧账目对账一致，报表不混算。

### 8.2 Admin

- L1 模型新增/修改/删除权限全部为 0。
- L2 模型只有平台管理员可写，且每次写有 `AuditEvent`。
- Admin 列表/搜索/导出按物业过滤，PII 不明文展示。
- 旧 `hotelapp` 菜单不再出现在“酒店业务模块”。

### 8.3 旧模块退役

- 旧写路由返回 410/403，不再写库。
- 每个旧业务域都有只读查询入口。
- 归档数据校验和、恢复演练通过。
- 删除前至少一个完整营业周期只读。

### 8.4 审计

- 前台排房、入住、退房、续住、换房、No-Show、押金、退款均写 `AuditEvent`。
- Admin 高危写操作写 `AuditEvent`。
- 审计事件包含操作人、物业、对象、动作、时间、原因。

---

## 9. 风险与待确认事项

### 9.1 需要业务拍板

1. 是否接受“服务级唯一写权威”的定义，并同意先冻结旧写入口，再按业务域迁移？
2. 餐饮/停车是否确认长期出范围，只保留第三方对接边界？
3. 旧 Client/Vip 迁移时，手机号是否允许一客多号、家庭共用号？这决定 `uq_guest_property_phone` 是否保留。
4. 旧 `FinanceRecord` 中的非房费收入/支出，由哪个新模块承接？
5. 夜审切换前，旧 `NightAudit` 历史数据是否需要在新报表中可查？查询口径由谁签字？

### 9.2 技术风险

| 风险 | 说明 | 建议 |
|---|---|---|
| PII 密钥回退 | `apps/guests/services.py` 在 `DJANGO_PII_KEY` 缺失时回退 `SECRET_KEY`；轮换 `SECRET_KEY` 会导致已有证件密文无法解密 | 生产环境强制配置 `DJANGO_PII_KEY`，并建立密钥轮换方案 |
| 旧 GET 写操作 | `delete_client`、`delete_accomodation` 等仍用 GET | 冻结前先改 POST 或直接 410 |
| Admin 一刀切 | 新入口未覆盖就关 Admin 写，可能导致业务无法操作 | 按批次收紧，先冻结高风险字段 |
| 旧入口静默重定向 | 旧 URL 重定向到新写接口，语义不清 | 明确 410，并提示新入口 |
| 删除先于归档 | 旧页面删除后历史数据无法查询 | 先补只读入口，再删页面 |
| CHANGELOG 缺失 | 本地 `3b2240a` 的 V1.1 P0 变更未见 CHANGELOG 记录 | 提交前补中文变更记录 |

---

## 附录 A：主要代码证据索引

| 文件 | 评审用途 |
|---|---|
| `hotel/settings.py:214-252` | 单店策略、旧业务菜单仍指向 `hotelapp` |
| `hotel/urls.py` | 旧 CRUD、入住、夜审、财务路由仍挂载 |
| `hotelapp/models.py` | 旧 Staff/Client/Vip/Room/Accommodation/FinanceRecord/NightAudit/Folio |
| `hotelapp/views.py:47-50` | `MODULE_REGISTRY` 关闭 food/order/park |
| `hotelapp/views.py:397-403` | `delete_client` GET 直接删除 |
| `hotelapp/views.py:976` 起 | `delete_accomodation` GET 冲账 |
| `hotelapp/views.py:1257` 起 | 旧房态直接修改 |
| `hotelapp/admin.py:20-100` | 旧 Admin 仅禁止删除，多数模型仍可改 |
| `apps/identity/admin.py:6-16` | 身份模型只读 |
| `apps/billing/admin.py` | Folio/Ledger/Payment 全字段只读；Refund 未注册 |
| `apps/guests/admin.py:11` | Guest 仅时间戳只读 |
| `apps/guests/models.py:35-40` | `uq_guest_property_phone` 唯一约束 |
| `apps/guests/models.py:104-152` | GuestDocument 密文/哈希/查重约束 |
| `apps/guests/services.py` | 证件加密、揭示、AuditEvent |
| `apps/inventory/admin.py:13-39` | Room/DailyRate/RoomTypeInventory 可写 |
| `apps/reservations/admin.py:13-27` | Reservation/Stay 可写 |
| `apps/reservations/front_desk_services.py:14` | 前台使用文件日志 |
| `apps/reservations/front_desk_services.py:289-356` | 前台直接创建 Payment/LedgerEntry |
| `apps/reservations/lifecycle_services.py:354-433` | 生命周期服务直接创建 Payment/LedgerEntry/Refund |
| `apps/reservations/services.py:146` | 预订服务直接创建 Folio |
| `apps/billing/models.py:104-115` | LedgerEntry 不可变账本 |
| `apps/core/models.py:43-112` | AuditEvent |
| `apps/core/models.py:114-168` | LegacyRecordLink |
| `apps/core/models.py:171-257` | BusinessDate、NightAuditRun |
| `apps/core/admin.py` | 仅注册 Property |

## 附录 B：评审边界

1. 本评审基于本地工作区 `3b2240a`，规划文档基线为 `b1e2776`；实施前需确认两者差异已合并。
2. 本评审只读检查 SQL 文件的表名与 INSERT 数量，未读取、复制或验证任何记录值；是否含真实敏感数据需数据负责人确认。
3. 本评审未启动完整运行环境做 UI 回归，Admin 可写性判断基于 `ModelAdmin` 权限方法静态检查。
4. 本评审不替代业务决策；第 12 节最终确认需业务、财务、前台、客房和管理层签字。
5. 本轮未修改业务代码、数据库或原规划文档。
