# PMS 3.0 全项目开发最终方案

> 文档性质：PMS 3.0 最终开发方案与任务执行基线（待负责人确认后冻结）
> 目标仓库：`wulei0214/django-hotel2`
> 基线分支：`main`
> V2 修订日期：2026-11-07
> 最新 main 基线：7a2c9b8c896adf8fcb5220d6c768586fc9ad3bbe
> 前置项目：PMS 2.0
> 重要门禁：本文件允许现在进行方案评审、任务拆解、代码现状盘点和准备工作；**PMS 3.0 正式业务代码开发必须等 PMS 2.0 最终验收报告结论为“通过”后启动**。当前仓库 A16 仍是开发者申报版，结论为“有条件整改”，不能视为最终验收通过。按 2026-11-07 架构审查及后续证据校准：Redis 已部署验证，G10 性能压测已完成；G09 多 worker 共享证据仍需阶段四复核；阶段四本地业务验收（含空库迁移）和 G14 独立验收签署仍未完成。本方案不得被解释为 PMS 2.0 已通过，也不得绕过该门禁。
>
> > 修订：2026-11-07 架构审查 —— 按 A16 申报版后续更新校准门禁现状：**G10 性能容量已完成**（A11 压测 6 场景 PASS：waitress 16 线程 + MySQL 4000 预订/4002 账本行数据集，P50/P95/P99 与错误率 0）；**Redis 5.0.14 已部署验证**（多进程共享/TTL 过期/故障连接捕获，2026-11-06）。剩余门禁为：阶段四用户本地业务验收（含空库全量迁移执行，DBA 授权待用户操作）与 G14 独立验收人指派签署；G09 多 worker 共享证据需在阶段四复核。门禁性质不变：上述完成并签署“通过”前，PMS 3.0 正式业务开发不启动。证据：`docs/acceptance/A16-final-acceptance-report.md`（§19 运行与部署行、§“进入 PMS 3.0 的门禁状态”）。
> 现状声明：本文件是目标设计与实施任务基线，不代表其中列出的字段、接口、页面或服务已存在。开发前必须以当时最新 `main` 的模型、迁移、服务、路由和测试为准，优先复用并补齐，禁止重复造表、重复造服务。

---

## 0. 执行摘要

PMS 3.0 的目标不是另起一套酒店管理系统，也不是把 PMS 2.0 页面换肤，而是在 PMS 2.0 已建立的业务权威、身份权限、账务审计和基础工作台之上，完成**数据模型可靠性、业务规则完整性、跨中心协同、运行时韧性、可观测性、自动化运营和长期可维护性**的系统升级。

PMS 3.0 必须保持以下方向：

1. 一个 PMS 业务系统、一套权威业务数据、一组唯一领域写服务；不建立平行新旧业务系统。
2. Django 模块化单体；不因版本升级拆分微服务，不引入没有明确收益的复杂基础设施。
3. PMS2 已完成且通过验收的能力默认继承；只对明确列出的缺口做增强。
4. Guest 是住客主档；Reservation.guest 是预订联系人，不等于实际入住人；实际入住人通过关联实体表达。
5. 员工登录身份、员工档案、物业任职、岗位授权彼此分离；权限管理不等于员工中心。
6. 账务采用追加凭证、冲销和审计，不允许直接改写历史账务事实。
7. 所有跨模块操作有事务边界、幂等策略、并发控制、状态机和审计证据。
8. 所有字段必须进入数据字典；所有接口必须进入契约；所有功能必须能映射到服务、页面、权限和测试。
9. 所有数据迁移采用 Expand → Backfill → Verify → Cutover → Archive → Contract；禁止未经核验直接删表删列。
10. 所有生产关键能力均有失败模式、恢复方式、监控指标和责任人。

### 0.1 PMS 3.0 的交付结果

最终应形成：

- 完整且无重复权威实体的数据模型和字段字典；
- 清晰的领域服务边界与单一写入口；
- 11 个业务中心的稳定闭环与跨中心流程；
- 统一 API、身份权限、错误码、Request ID 和审计链；
- 可靠的 MySQL 8 数据迁移、约束、索引和并发策略；
- Redis 在缓存、限流、幂等协调等场景的明确职责和故障降级；
- 后台任务、夜审、通知待办、报表的可靠执行机制；
- 统一前端设计系统、工作台导航、详情与操作交互；
- 自动化测试、E2E、安全、性能、恢复和发布证据；
- 可持续维护的开发文档、运维手册、数据治理台账和发布流程。

### 0.2 不在本方案默认范围内的内容

除非负责人另行批准，不纳入本期：

- 微服务拆分、Kubernetes 集群化；
- 全量重写 Django 或更换前后端技术栈；
- 多物业集团化运营、跨物业调拨和复杂集团财务；
- 自建支付清算、银行卡存储或支付卡数据处理；
- 复杂收益管理/动态定价算法；
- AI 自动决策、自动识别证件或自动批准财务操作；
- 餐饮 POS、停车硬件、门锁、PABX、渠道管理器的深度集成；
- 未经业务确认的会员积分商城、营销自动化、CRM 群发；
- 未经批准的顾客自助注册、员工自助注册或顾客 PMS 登录账户。

上述事项可建立未来扩展接口，但不能先建空壳模块、占位菜单或无业务依据的表。

---

## 1. 版本门禁、范围和优先级

### 1.1 PMS 2.0 到 PMS 3.0 的启动门禁

PMS 2.0 最终验收标准是上位门禁。启动 PMS 3.0 正式开发前，必须完成：

- A04：MySQL 8 空库全量迁移和已有库升级验证；
- A09：MySQL 约束、索引、事务、并发和数据核对证据；
- G09：Redis 部署、多 worker 共享、TTL、故障降级和恢复验证；
- G10：负责人冻结容量目标并完成性能压测；
- 阶段四：用户本地实际业务验收、九角色登录、备份恢复和必要数据治理；
- G14：独立验收负责人复核并签署“通过”；
- PMS2 最终验收报告结论为“通过”。

在门禁未通过时只允许：
- 方案评审、字段/接口设计、任务依赖拆分；
- 只读代码审查、现状矩阵和测试计划准备；
- 修复 PMS2 验收阻断项；
- 不改变 PMS2 既定范围的必要文档修订。

不允许：
- 以 PMS3 名义新增业务模型、写 API、正式页面或改变既有业务行为；
- 把 PMS2 未验收功能改名为 PMS3 功能以绕开验收；
- 在 PMS2 最终验收前删除旧数据、迁移生产数据或切换业务权威。

### 1.2 任务优先级

| 优先级 | 定义 | 执行要求 |
|---|---|---|
| P0 | 安全、资金、数据完整性、身份权限、不可逆迁移、核心交易正确性 | 必须先完成；未通过阻断后续依赖 |
| P1 | 核心业务闭环、跨模块状态一致性、运行韧性、关键数据治理 | 按阶段完成；未通过不得发布 |
| P2 | 性能、运营效率、可观测性、管理分析和体验完善 | 按冻结范围交付 |
| P3 | 非关键体验优化、低频便利能力 | 进入后续迭代，不影响核心正确性 |

### 1.3 状态定义

所有任务只能使用以下状态：

- 未盘点；
- 已盘点待设计；
- 设计待确认；
- 待开发；
- 开发中；
- 已开发待测试；
- 测试不通过；
- 已开发待验收；
- 已验收；
- 阻塞；
- 已废弃待退役；
- 已退役。

“已开发”不等于“已验收”；“已提交”不等于“已完成”。

---

## 2. 当前代码基线与实施约束

本次 V2 复核采用的最新 main 基线为：

`7a2c9b8c896adf8fcb5220d6c768586fc9ad3bbe`（2026-11-07，公开预订生命周期整改）。此前架构审查基线 `635d9dd` 仍是 §3.4、AR-01~AR-11 等架构实证的历史依据，不得误认为它是本次最新代码基线。最新公共预订整改另见 `docs/audit/phase0/90-public-booking-order-lifecycle-review.md`。BASE-01 启动时仍须重新 fetch main 生成新基线清单。

该 SHA 仅是本方案编制时的参考点；实际开发启动时必须重新 fetch main，生成新的基线清单。仓库当前已存在下列主要领域目录（实际文件和调用关系需在启动时再次核对）：

- `apps/identity`：员工身份、任职、授权、邀请、会话；
- `apps/guests`：Guest、联系方式、证件、会员关系；
- `apps/inventory`：房间、房型、价格、库存、清洁维修相关模型；
- `apps/reservations`：预订、入住、房间段、住客关联、生命周期服务；
- `apps/billing`：Folio、费用、收款、退款、账务、审批、夜审；
- `apps/core`：物业、营业日、审计、幂等、限流、通知等公共能力；
- `apps/api`：业务中心 API、报表、审计、通知、OpenAPI；
- `templates/pms`：PMS 工作台页面；
- `hotel`：Django 配置与总路由；
- `docs`：设计、开发、审计和验收文档。
- `hotelapp`：PMS1 遗留模块（11 个旧模型、旧视图、旧事件总线与请求审计中间件；35 条旧写路由已 410 封死并有 `apps/identity/test_module_gates.py` 护栏；`/no_show_one/` 为活跃例外，见审查报告 AR-01）。

历史 A16 报告中的 SQLite/MySQL 729 项测试结果属于较早提交，不可直接作为最新 main 的测试结论；A16 原始表格关于 Redis 未部署、性能未执行的记录也已被后续 2026-11-07 审查证据更新。最新可核验的公共预订整改报告记录 SQLite 全量 887 项通过，新增订单生命周期测试 11 项；本次复核未取得最新 7a2c9b8 对应的 MySQL 8 全量回归报告，因此 MySQL 8、空库迁移、升级库和阶段四用户验收仍按未完成门禁处理。

### 2.1 启动时必须重新执行的现状盘点

| 编号 | 盘点内容 | 产物 |
|---|---|---|
| BASE-01 | 最新 main SHA、提交链、未合并变更 | 版本基线报告 |
| BASE-02 | Django App、模型、migration、表、索引、约束 | 数据库对象清单 |
| BASE-03 | 所有 URL、HTTP 方法、视图、服务调用链 | API/路由矩阵 |
| BASE-04 | 页面、模板、静态资源、菜单和页面动作 | 页面/菜单矩阵 |
| BASE-05 | 所有核心模型写入点，包括 View、Admin、Command、Signal、Task | 写入口调用图 |
| BASE-06 | 权限码、角色、对象范围、物业范围和字段揭示权限 | 权限矩阵 |
| BASE-07 | 测试文件、用例、CI 工作流、数据库引擎 | 测试覆盖矩阵 |
| BASE-08 | 旧模型、旧 URL、旧模板、旧权限和遗留字段 | 退役依赖矩阵 |
| BASE-09 | 配置、密钥、Redis、后台任务、日志、健康检查 | 运行环境矩阵 |
| BASE-10 | 数据规模、异常数据、重复键、孤儿关联和账务差异 | 数据质量报告 |

禁止根据旧文档中的代码路径直接开发；必须先确认文件、类、函数和调用关系仍然存在。

> 修订：2026-11-07 架构审查 —— BASE-01 至 BASE-10 的架构维度首轮盘点以 `635d9dd` 为基线，结果见 `docs/audit/phase0/80-architecture-review.md`；V2 在最新 `7a2c9b8` 上追加公共预订专项核验，见 `docs/audit/phase0/90-public-booking-order-lifecycle-review.md`。两份报告的基线不同，引用时必须注明 SHA。BASE-05（写入口调用图）与 BASE-08（退役依赖矩阵）已有关键实证：35 条旧写路由 410 封死，但 `/no_show_one/` 为活跃旧写入口例外，已并入 MIG-06 范围与 DEC-24 决策。

---

## 3. 总体架构目标

### 3.1 架构形态

继续采用 Django 模块化单体：

- Web 页面层：模板/前端静态资源/交互；
- API 层：认证、输入校验、DTO、响应封套；
- 应用服务层：用例编排、事务边界、权限上下文；
- 领域服务层：状态机、金额规则、库存规则、生命周期；
- 数据访问层：Django ORM、数据库约束、索引；
- 基础设施层：MySQL、Redis、文件存储、后台任务、日志和监控。

禁止在模板、API View、Django Admin、Management Command 中散落业务写逻辑。它们只能调用应用服务/领域服务。

### 3.2 依赖方向

`UI/API → Application Service → Domain Service → Model/Repository`

基础设施通过明确接口供服务使用。领域服务不得依赖模板或 HTTP Request。公共模块不得反向依赖具体业务 App。

### 3.3 统一约束

- 一个业务事实只有一个权威模型；
- 一个领域动作只有一个权威写服务；
- 所有业务写操作都在事务边界内；
- 所有关键写操作都要有幂等和并发策略；
- 所有权限由服务端逐请求校验；
- 所有跨模块关联使用真实外键或稳定业务标识；
- 所有金额使用 Decimal/数据库精确数值；
- 所有日期时间遵循物业时区与营业日语义；
- 所有状态变化有明确状态机；
- 所有不可逆动作有审批/确认/审计/恢复策略。

### 3.4 已确认的架构现状与约束继承（2026-11-07 架构审查）

> 修订：2026-11-07 架构审查 —— 本节为 PMS 3.0 必须继承的架构原则，全部经 `docs/audit/phase0/80-architecture-review.md` 实证确认（基线 `635d9dd`）。后续任务包设计与验收以本节为门槛，不得在 3.0 开发中回退。

1. **依赖方向单向**（已守住，必须保持）：UI/API → 应用服务/领域服务 → 模型。实证：全部服务层零 `import apps.api`；`apps/identity` 非测试代码仅依赖 `apps/core`。3.0 新增代码沿用此方向；PM-05 的 CI 依赖规则检查以本矩阵为基线，新增违例即失败。
2. **单一写入口**（4/6 权威实体已收口，2 处登记待收口）：Payment/LedgerEntry/Refund/ApprovalRecord 的创建已完全收口于 BillingService（`.create()` 仅存在于 `billing/services.py`/`services_v2.py`）；Reservation 创建收口于 `apps/reservations/services.py`。登记待收口项：Guest 主档双路径（AR-03，公共预订内联直写）、Folio 开关单跨域写权威（AR-04/AR-05）、Room 状态直写（AR-06）。以上并入 SVC 阶段对应任务包（SVC-02/SVC-04/SVC-05）完成收口，3.0 验收时按"每个权威实体一个写服务"复检。
3. **物业隔离三层一致**（已确认，必须保持）：中间件（`PropertyContextMiddleware` 服务端解析 DEFAULT 物业任职，无任职即拒绝+审计）、视图/API（`request.property` 注入，禁止信任客户端 property_id）、服务层（`property_obj` 必参）+ 模型层（全表 property FK、物业范围唯一键）。3.0 一切新服务签名必须延续 `property_obj` 必参模式；多物业切换维持 DEC-20 隔离，不启用。
4. **审计覆盖**（已确认，必须扩展）：`AuditEvent.record` 现有 70 调用点/15 文件，AuditEvent 采用不可变 QuerySet，配套 StatusTransition/NightAuditRun/BreakGlassRecord 运行事实。3.0 CMP-03 事件覆盖率矩阵以此为底册增量登记；数据库级不可变（CMP-01）在应用层之上补 DB 层防护。
5. **已知漂移白名单与清偿义务**：`core/services_notifications.py` 对业务 App 的模块级反向依赖（AR-02）、`core/labels.py` lazy 依赖（AR-07）、API 层 NotificationRead 直写（AR-08）为已登记存量漂移，分别在 WN/UI/API 任务包内清偿；清偿前不得作为"公共层可反向依赖业务"的先例引用。billing↔reservations 服务层双向耦合（AR-09）为演进项，3.0 SVC 阶段经领域事件解耦。

---

## 4. 领域模型与数据治理总方案

### 4.1 领域边界与权威关系

| 领域 | 权威实体 | 业务含义 | 禁止重复建模 |
|---|---|---|---|
| 平台/物业 | Property | 物业及运行配置 | 不为每个业务模块复制物业表 |
| 员工身份 | User、StaffProfile | 登录身份与员工档案分离 | 不用手机号直接作为员工登录主键 |
| 员工任职 | PropertyStaffMembership | 员工与物业的任职关系 | 不把全局员工状态等同于单物业任职状态 |
| 岗位授权 | StaffRoleAssignment/Role/Permission | 任职期间岗位权限 | 不从 User.groups 作为第二套授权权威 |
| 顾客主档 | Guest | 自然人/客户档案 | 不继续以 Client/Vip 作为活动主档 |
| 顾客联系方式 | GuestPhone 等 | 电话、验证、共享关系和偏好 | 不把共享手机号等同于唯一身份 |
| 证件 | GuestDocument | 加密证件和揭示审计 | 不明文保存证件号 |
| 会员 | GuestMembership、MembershipLevelHistory | Guest 的会员关系和等级历史 | 不复制 Guest 个人资料 |
| 房间资源 | Room、RoomType | 物理房间和房型 | 不将房间号作为跨物业全局唯一 |
| 价格/库存 | RatePlan、DailyRate、RoomTypeInventory、InventoryHold | 价格、逐日库存和占用 | 不允许页面自行计算权威可售数 |
| 预订 | Reservation、ReservationRoom 等 | 预订合同及预订房间 | 不将联系人等同于所有入住人 |
| 入住履约 | Stay、StayGuest、StayRoomSegment | 实际入住、同住人和房间时段 | 不用 Reservation 状态代替 Stay 状态 |
| 账务 | Folio、ChargeLine、Payment、Refund、LedgerEntry | 账单、收款、退款和账本 | 不以 Payment 汇总代替总账口径 |
| 营业日/夜审 | BusinessDate、NightAuditRun | 营业日与夜审运行事实 | 不以自然日替代营业日 |
| 清洁维修 | HousekeepingTask、MaintenanceTicket、RoomInspection | 作业任务和房间可用性 | 不用一个 Room.status 表达全部工作状态 |
| 审计 | AuditEvent、StatusTransition | 业务审计与状态历史 | 不把普通应用日志当业务审计 |
| 待办通知 | WorkItem/Notification 或现有通知模型 | 工作流任务和通知投递 | 不重复保存源业务状态 |
| 数据迁移 | LegacyRecordLink | 旧数据来源和映射 | 不删除来源追溯链 |

上述名称是领域语义基线，不代表所有表都需新增。开发前必须检查现有模型并优先复用。

### 4.2 字段数据字典要求

每个字段必须登记：

- App、模型、字段名；
- 中文业务名称；
- 业务定义和取值来源；
- 类型、长度、精度、NULL、默认值；
- 主键/外键/唯一/检查约束；
- 索引及查询场景；
- 敏感级别（公开、内部、个人信息、敏感个人信息、财务）；
- 是否允许前端提交；
- 唯一写入服务；
- 读取角色和字段遮罩规则；
- 审计要求；
- 保留期限和删除/归档规则；
- 数据迁移来源及兼容期。

禁止字段只有英文名无业务定义；禁止多个字段表达同一事实却没有主从规则。

### 4.3 数据库通用规则

- MySQL 8 为目标引擎，InnoDB、utf8mb4；
- 金额字段使用 Decimal，明确币种、精度和舍入；
- 日期范围统一采用左闭右开，明确物业时区；
- 物业范围唯一键必须包含 property；
- 对可空唯一键、NULL 语义、复合索引长度进行 MySQL 实测；
- 历史账务、审计、审批和订单使用保护性删除策略；
- 关键写操作必须有数据库约束作为最后防线；
- 不依赖“先查再写”实现并发唯一性；
- migration 不修改已发布历史文件；
- 大表迁移必须评估锁表、DDL 隐式提交和回滚窗口；
- 所有数据清理脚本必须 dry-run、范围白名单、计数预览、二次确认、审计和回滚/恢复说明。

---

## 5. PMS 3.0 全业务中心开发方案

每个中心必须交付：业务流程、字段字典、领域服务、API 契约、前端页面、权限矩阵、审计规则、数据迁移（如适用）、自动化测试和验收报告。

### 5.1 员工中心（Employee Center）

**职责边界：** 管理员工身份档案、物业任职和员工生命周期；不负责权限策略定义和权限码管理。权限管理中心负责角色/权限配置，员工中心负责把经批准的角色分配给员工。

#### 功能需求（REQ 编号，不是执行任务 ID）

| 需求 ID | 优先级 | 功能需求 | 规则/完成条件 |
|---|---|---|---|
| REQ-EMP-01 | P0 | 员工身份/档案/任职/授权模型核对 | 明确 User、StaffProfile、Membership、Assignment 关系 |
| REQ-EMP-02 | P0 | 员工编号规则 | 物业范围唯一；正式工号不可随意修改；离职工号保留且不可复用 |
| REQ-EMP-03 | P0 | 再入职模型 | 决策“恢复原任职”还是“新建任职记录”；不得被简单唯一约束阻断 |
| REQ-EMP-04 | P1 | 入职/邀请/激活 | 管理员创建邀请；一次性 token、过期、撤销、使用审计 |
| REQ-EMP-05 | P1 | 调岗/换部门/角色变更 | 记录生效时间、原因、操作者、原值和新值 |
| REQ-EMP-06 | P1 | 暂停/离职/恢复 | 立即撤销有效会话和岗位授权；保留历史 |
| REQ-EMP-07 | P1 | 员工详情时间线 | 任职、角色、账号、密码重置和状态历史 |
| REQ-EMP-08 | P1 | 员工批量导入/导出 | 仅批准字段；预览、校验、冲突报告、权限和审计 |
| REQ-EMP-09 | P2 | 员工数据质量盘点 | 未绑定 User、无 Membership、重复工号和失效授权报表 |
| REQ-EMP-10 | P2 | 账号安全操作 | 管理员重置密码、强制改密、禁用、会话撤销 |

#### 关键字段设计（新增前必须查重）

建议核对/补齐：
- `PropertyStaffMembership.employee_no`：物业范围工号；
- `PropertyStaffMembership.employment_status`：当前物业任职状态；
- `start_at`、`end_at`：任职时间；
- `rehire_of_id` 或等效关系：再入职来源（只有业务确认后新增）；
- `StaffRoleAssignment.effective_from/effective_to`：授权有效期；
- `StaffRoleAssignment.reason_code/reason_text`：授权原因（现状需核查，不得假设已有）；
- `Invitation.expires_at/used_at/revoked_at`：邀请生命周期。

> 修订：2026-11-07 架构审查 —— 按 `635d9dd` 实证核查本节字段现状：`StaffRoleAssignment` 现无 `effective_from/effective_to`、`reason_code/reason_text` 字段（EMP 关键字段与 IAM-07 为新建，前提成立）；`PropertyStaffMembership`/`Invitation` 字段以启动时 BASE-02 盘点为准。`billing.cashier_shift_manage` 权限码已在权限注册表注册（registry.py:474），员工中心授权调用无需重复建模。

#### 服务/API/页面

- 服务：`EmployeeLifecycleService` 为员工、任职和生命周期唯一写入口；角色配置调用 Authorization/RoleAssignment 服务。
- API：员工列表/详情、邀请创建/撤销/重发、激活、任职变更、角色分配、暂停、离职、再入职、密码重置、会话撤销、数据质量检查。
- 页面：员工列表、员工详情、任职时间线、邀请状态、岗位授权摘要、生命周期操作弹窗。
- 禁止：员工中心自行创建 Permission、直接修改 User.groups、直接写 StaffProfile/Membership 绕过服务。

#### 验收

- 同一员工跨物业任职隔离正确；
- 离职后不能凭旧会话继续操作；
- 工号唯一且离职保留；
- 再入职不会覆盖历史；
- 邀请 token 不能重放、过期或跨员工使用；
- 操作均有审计及权限测试。

### 5.2 Guest 360 客人中心

**职责边界：** Guest 为客人主档，聚合联系方式、证件、会员、预订、入住、账单和服务历史。不得把预订联系人当成所有实际入住人。

#### 功能需求（REQ 编号，不是执行任务 ID）

| 需求 ID | 优先级 | 功能需求 | 规则/完成条件 |
|---|---|---|---|
| REQ-GST-01 | P0 | Guest 唯一主档与重复识别策略 | 手机号不能单独证明自然人唯一 |
| REQ-GST-02 | P0 | 联系方式可信度/共享手机号 | 共享号码人工复核、primary_guest_key 规则明确 |
| REQ-GST-03 | P0 | 证件密钥版本与轮换 | 密钥版本可识别，轮换可恢复，禁止无密钥解密 |
| REQ-GST-04 | P1 | Guest 合并工作流 | 提案、审核、合并、撤销/补偿、关联迁移和审计 |
| REQ-GST-05 | P1 | 实际入住人关系 | ReservationGuest、StayGuest、StayRoomSegment 真实关联 |
| REQ-GST-06 | P1 | Guest 时间线 | 预订、入住、房间段、账单、会员、投诉/服务事件 |
| REQ-GST-07 | P1 | 客人偏好与联系授权 | 仅纳入有明确用途的字段，记录来源和同意版本 |
| REQ-GST-08 | P1 | 证件留存和到期清理 | retention_until、法律保留例外、删除任务审计 |
| REQ-GST-09 | P2 | Guest 数据质量 | 重复、缺字段、共享手机号、失效证件统计 |
| REQ-GST-10 | P2 | 客人档案导出 | 最小化、审批/理由、字段脱敏、审计 |

#### 字段设计

需评估而非默认新增：
- Guest：`gender`、`nationality`、`preferred_language`、`date_of_birth`（涉及敏感和必要性审查）；
- GuestPhone：`verified_at`、`shared_review_required`、`primary_guest_key`、`source`；
- GuestDocument：`key_version`、`retention_until`、`doc_type`、`doc_no_hash`；
- GuestMergeRequest：source_guest、target_guest、reason、status、reviewer、approved_at、executed_at、reversal reference（仅在模型不存在时设计）。

必须为证件类型+证件哈希建立符合物业/证件政策的索引，避免明文证件号索引。

> 修订：2026-11-07 架构审查 —— 按 `635d9dd` 实证校准本节现状：①`GuestDocument.key_version` 确认不存在（GST-03 为新建任务）；②`GuestDocument.retention_until` 已存在（`apps/guests/models.py:134`），GST-08 任务聚焦到期清理任务、法律保留例外与审计，不含加字段；③`GuestMergeRequest` 模型确认不存在，GST-04 的"仅在模型不存在时设计"前提成立，决策依赖 DEC-03/DEC-06；④Guest 主档写入口现状为双路径（GuestService 与公共预订内联直写，审查报告 AR-03），本中心 SVC-02 任务须完成收口后方可叠加合并工作流。

#### 服务/API/页面

- 服务：GuestService、GuestContactService、GuestDocumentService、GuestMergeService、GuestTimelineQueryService。
- API：Guest 列表/详情/时间线、联系方式验证/共享复核、证件揭示、合并提案/审批/执行、数据质量统计。
- 页面：Guest 360 总览、联系方式与证件分区、会员卡片、预订入住时间线、账单摘要、合并审核队列。
- 证件揭示必须有独立权限、理由、限流和审计；API 默认不返回完整证件。

#### 验收

- 预订联系人和实际入住人不混淆；
- 共享手机号不自动合并档案；
- Guest 合并后所有历史 FK/业务引用可追溯；
- 密钥轮换后历史密文仍可按策略解密；
- 到期清理不误删法律保留数据；
- 时间线按物业时区和真实事件时间排序。

### 5.3 房态与库存中心（Room Status & Inventory）

**职责边界：** Room 是物理资源；RoomType 是产品分类；库存和价格服务决定可售性；清洁/维修工作状态与房态分开建模，再通过规则计算可售状态。

#### 功能需求（REQ 编号，不是执行任务 ID）

| 需求 ID | 优先级 | 功能需求 | 规则/完成条件 |
|---|---|---|---|
| REQ-INV-01 | P0 | 可售库存唯一计算规则 | 逐日房型库存、已售、冻结、停售、维修和占用一致 |
| REQ-INV-02 | P0 | 并发库存锁与版本控制 | 乐观版本/行锁/唯一约束组合，不能超卖 |
| REQ-INV-03 | P0 | InventoryHold 生命周期 | 创建、确认、释放、过期、清理、重试可追溯 |
| REQ-INV-04 | P1 | 房态工作台 | 房间、房型、楼层、清洁、维修和入住状态视图 |
| REQ-INV-05 | P1 | 维修/清洁影响库存 | 状态变化通过服务同步，不直接改库存计数 |
| REQ-INV-06 | P1 | 价格日历和 RatePlan | 有效期、币种、税费、关闭日、版本历史 |
| REQ-INV-07 | P1 | 房间自然排序 | 楼栋/楼层/房号排序规则，不能纯字符串排序 |
| REQ-INV-08 | P1 | 批量房态操作 | 预览影响、权限、理由、逐项结果和审计 |
| REQ-INV-09 | P2 | 库存差异监控 | 每日核对 reservation/hold/room inventory |
| REQ-INV-10 | P2 | 价格变更审计 | 记录旧值、新值、原因、生效时间和操作者 |

#### 字段与约束评估

- RoomType：`sort_order`（确有业务排序需求时新增）；
- Room：楼栋/楼层/房号自然排序字段，避免破坏现有字符串房号；
- RoomTypeInventory：保留 `out_of_service`、`stop_sell`、`version` 等既有语义，核对并发写入；
- DailyRate：价格版本/历史记录或独立 RateChangeLog；
- InventoryHold：`expires_at`、`status`、`idempotency_key`、物业/日期索引；
- 所有计数必须由服务计算或维护，不允许前端直接传入可售数量。

#### 服务/API/页面

- 服务：InventoryAvailabilityService、InventoryHoldService、RoomStatusService、RatePlanService、InventoryReconciliationService。
- API：房态看板、房间详情、库存日历、可售查询、冻结/释放、停售/恢复、价格查询/变更、库存差异报告。
- 页面：房态总览、楼层视图、日历库存、价格日历、维修/清洁状态提示和批量操作。
- 价格和库存查询必须返回计算版本/时间点，便于定位竞态问题。

#### 验收

- 并发预订不超卖；
- hold 过期释放不会释放已确认预订；
- 维修中、停用、停售房间不会被错误销售；
- 清洁完成不等于自动可售，需满足检查/放行规则；
- 价格修改有历史、理由和权限；
- MySQL 并发测试覆盖锁冲突和重试。

### 5.4 会员中心（Membership）

**职责边界：** 会员是 Guest 的关系/权益，不是第二份顾客主档。

#### 功能需求（REQ 编号，不是执行任务 ID）

| 需求 ID | 优先级 | 功能需求 | 规则/完成条件 |
|---|---|---|---|
| REQ-MEM-01 | P0 | Guest 与会员关系唯一性 | 一位 Guest 在同一会员体系下的关系规则明确 |
| REQ-MEM-02 | P1 | 会员等级变更服务 | 权限、原因、生效时间和历史不可覆盖 |
| REQ-MEM-03 | P1 | 暂停/恢复/注销 | 状态机和重新激活规则 |
| REQ-MEM-04 | P1 | 会员历史与权益摘要 | 只展示已批准范围内真实权益 |
| REQ-MEM-05 | P2 | 会员标签/偏好 | 仅基于合法来源和业务用途 |
| REQ-MEM-06 | P2 | 会员报表 | 会员数量、等级变动和活跃口径冻结 |

字段评估：Membership status、level、effective_from/to、source、reason、changed_by；历史使用独立 history，不覆盖旧等级。不得擅自引入积分、储值、折扣钱包等财务实体。

服务/API/页面：GuestMembershipService；会员列表、详情、等级历史、暂停/恢复、关联修复；会员详情从 Guest 进入，不创建另一套 Guest profile。

验收：重复关联被阻止；Guest 合并时会员关系处理正确；等级历史不可篡改；无会员权益时不显示虚构权益。

### 5.5 预订与入住中心（Reservation & Stay）

**职责边界：** Reservation 表示预订合同；Stay 表示实际履约入住；ReservationGuest 和 StayGuest 表示人员关系；StayRoomSegment 表示实际房间和时间段。

#### 功能需求（REQ 编号，不是执行任务 ID）

| 需求 ID | 优先级 | 功能需求 | 规则/完成条件 |
|---|---|---|---|
| REQ-RSV-01 | P0 | Reservation/Stay 状态机统一 | 状态定义、合法迁移、终态规则明确 |
| REQ-RSV-02 | P0 | 预订并发与幂等 | 公共/后台/前台入口统一服务，防重复和超卖 |
| REQ-RSV-03 | P0 | 取消政策快照 | 创建时保存适用政策版本、计算结果和时点 |
| REQ-RSV-04 | P0 | 押金政策版本与事实快照 | 预订确认时保存适用政策和金额规则 |
| REQ-RSV-05 | P1 | 多房预订履约 | 部分入住、部分退房、换房和取消独立处理 |
| REQ-RSV-06 | P1 | Walk-in 业务决策 | 明确是否支持无预订直接入住；若支持，建立合法来源和账务链；若不支持，明确拒绝入口 |
| REQ-RSV-07 | P1 | No-show 与过期释放统一 | 合并重复命令逻辑，单一调度入口和锁 |
| REQ-RSV-08 | P1 | 延住/换房/提前离店 | 重新核验库存、价格、押金、账单和房间段 |
| REQ-RSV-09 | P1 | 预订变更历史 | 保存旧值、新值、原因、操作者和政策影响 |
| REQ-RSV-10 | P0 | 公共预订安全与生命周期 | 访问令牌、速率限制、库存占用和订单查询保护 |
| REQ-RSV-11 | P2 | 预订来源统计 | 来源代码、渠道、活动等口径冻结 |
| REQ-RSV-12 | P2 | 订单/入住异常队列 | 超期、状态冲突、缺少入住人、房间段断链 |

#### 字段与规则

评估补齐：
- Reservation：`cancellation_policy_snapshot`、`deposit_policy_snapshot`、`cancelled_at`、`cancel_reason`、`penalty_amount`；
- CancellationPolicy：版本唯一键、生效时间和历史；
- DepositPolicy：版本、有效期和审批；
- Reservation：幂等字段与公共创建流程统一接入通用幂等服务；
- Payment：`business_date`、`received_by`（若业务事实确实由 Payment 承载）；
- 订单联系人与实际入住人分开存储；
- 预订取消、No-show、释放 hold 和退款之间明确事件顺序。

不得以修改 Reservation 状态代替退款；不得删除已产生账务事实的订单；不得把顾客传入价格当权威价格。

> 修订：2026-11-07 架构审查 —— 按 `635d9dd` 实证校准本节现状：①`CancellationPolicy`/`DepositPolicy` 模型已存在且按 rate_plan 版本化（`apps/reservations/models.py:441/478`，含 `version`、`penalty_*`、ordering `("rate_plan_id", "-version")`），RSV-03/RSV-04 任务聚焦 Reservation 侧 `cancellation_policy_snapshot`/`deposit_policy_snapshot` 字段与创建时固化流程，**政策模型不重建**；②No-show 现状存在双入口：PMS2 权威命令 `/api/v1/reservation-rooms/<id>/no-show` 与活跃旧写入口 `/no_show_one/`（`hotelapp/views.py:1357`，直写旧 Accommodation/Room 并走旧权限链，审查报告 AR-01）——RSV-07"单一调度入口"的达成以 MIG-06 冻结 `/no_show_one/` 为前置，业务确认事项见 DEC-24；③预订创建写入口已收口（Reservation `.create()` 仅 `apps/reservations/services.py:132`，含幂等键+请求指纹+15 分钟过期），RSV-02 为契约推广与并发复测，非从零建设。

**REQ-RSV-13～REQ-RSV-18（支付链路与恢复，均为 PMS3 生产化任务，不代表当前已实现）**

| 需求 ID | 优先级 | 功能需求 | 规则/完成条件 |
|---|---|---|---|
| REQ-RSV-13 | P0 | 支付渠道适配与环境隔离 | 定义 provider adapter；生产环境拒绝 mock；密钥独立管理；渠道/币种/商户配置可审计 |
| REQ-RSV-14 | P0 | 支付意图与外部交易状态机 | 区分创建、处理中、成功、失败、关闭、未知；未知状态不得再次扣款 |
| REQ-RSV-15 | P0 | 回调验签与幂等 | 验签、时间窗、防重放、外部交易号唯一、重复/乱序回调无副作用 |
| REQ-RSV-16 | P0 | 支付结果查询与超时恢复 | 通过渠道主动查询/回调重放恢复；客户端超时不推定支付失败 |
| REQ-RSV-17 | P0 | 订单/支付/库存 Hold 协同 | 支付成功、过期释放、取消、并发回调竞态有事务/锁/补偿规则 |
| REQ-RSV-18 | P1 | 渠道结算与 PMS 账务对账 | 外部交易、Payment、LedgerEntry、退款逐笔对账；差异进入人工队列，不自动改账 |

#### 公共预订生命周期专项要求（承接最新 main：7a2c9b8）

- **已实现并有证据**：256-bit 随机令牌；数据库保存 SHA-256 摘要；查询只接受 X-Booking-Token 请求头；令牌不得经 URL query；错误凭证返回不泄露订单信息的 404；订单查询限流和审计；创建成功一次性展示令牌并可复制；找回后令牌脱敏；PENDING 才允许继续支付/自助取消；其他状态仅查看；重复支付意图返回同一意图；mock 回调路径有测试。
- **仍需在 PMS3 明确/验收**：真实支付服务商适配、正式环境禁用 mock、回调签名密钥轮换、支付渠道主动查询、退款渠道状态同步、渠道结算对账、支付超时与订单/库存 hold 的竞态处理、跨 worker 幂等与并发测试、支付渠道故障告警及人工差异队列。
- **严禁混淆**：PaymentIntent、外部渠道交易、Payment、LedgerEntry、Reservation 状态是不同业务事实；任何一个状态成功不能自动推定其他状态已成功。支付成功但 PMS 尚未入账时，必须可通过渠道查询/回调重放/对账任务恢复，不能再次扣款。
#### 服务/API/页面

- 服务：ReservationService、ReservationPolicySnapshotService、StayLifecycleService、RoomAssignmentService、ReservationExpiryService。
- API：可订查询、创建、详情、修改、取消、确认、排房、入住、续住、换房、退房、No-show、过期释放、订单查询。
- 页面：预订列表/详情、预订时间线、房间分配、入住登记、住中操作、退房结账、异常队列。
- 公共预订和员工工作台使用同一领域服务，但认证/授权/DTO/限流策略不同。公共订单访问凭证只用于订单级访问，不创建 Guest User/Django User/PMS 登录账户。支付供应商未完成接入前，模拟支付只允许受控测试环境使用，不能标记为生产支付能力。

#### 验收

- 同一请求重试不重复创建预订；
- 并发不超卖；
- 取消政策按创建时快照执行；
- 多房订单部分入住/退房准确；
- 实际入住人和预订联系人不混淆；
- 延住、换房会重新核算库存和价格；
- 过期释放命令只有一个权威实现；
- 订单查询不泄露其他客人信息；Token 仅通过 X-Booking-Token 请求头传递，禁止 URL query；数据库只存 SHA-256 摘要；创建时一次性展示，找回后脱敏；错误凭证限流并审计；支付回调验签、幂等和对账。

### 5.6 财务与账单中心（Finance & Billing）

**职责边界：** 账单和总账事实由 Billing 服务管理；页面不直接写 Payment、Refund、LedgerEntry 或 Folio。

#### 功能需求（REQ 编号，不是执行任务 ID）

| 需求 ID | 优先级 | 功能需求 | 规则/完成条件 |
|---|---|---|---|
| REQ-FIN-01 | P0 | 账本追加不可变 | 账务只追加，错误通过冲销/调整凭证修复 |
| REQ-FIN-02 | P0 | 退款状态机补齐 | 申请、待审批、批准待执行、执行中、成功、失败、拒绝 |
| REQ-FIN-03 | P0 | 审批与执行职责分离 | 审批人不得执行自己批准的高风险操作，遵循 SoD |
| REQ-FIN-04 | P0 | 并发幂等账务写入 | 唯一凭证键、事务和重试，不出现重复收款/退款 |
| REQ-FIN-05 | P1 | 营业日归属 | Payment、CashierShift、LedgerEntry 的 business_date 口径明确 |
| REQ-FIN-06 | P1 | Folio Transfer 配对 | 转出/转入双边凭证、property、currency、transfer_group 一致 |
| REQ-FIN-07 | P1 | 账务对账 | ChargeLine、TaxLine、Payment、Refund、LedgerEntry、Folio 核对 |
| REQ-FIN-08 | P1 | 发票记录 | 若纳入业务范围，定义发票状态、外部编号、幂等和冲红关系 |
| REQ-FIN-09 | P1 | 收银班次 | 开班、交班、结班、差异、复核和营业日关联 |
| REQ-FIN-10 | P2 | 财务报表 | 以 LedgerEntry 和正式科目口径为准 |
| REQ-FIN-11 | P2 | 财务导出 | 字段最小化、权限、理由、审计和导出水印（如批准） |

#### 字段/模型评估

- Refund：状态、approved_by、approved_at、execution_started_at、executed_at、failure_code、failure_message（脱敏）；
- ApprovalRecord：申请人、审批人、执行人、SoD、状态、幂等、拒绝/过期/重申请；
- Payment：business_date、received_by（按事实归属确定）；
- CashierShift：business_date、opened_by、closed_by、差异说明；
- FolioTransfer：property、currency、transfer_group、source_entry、target_entry；
- LedgerEntry：唯一业务凭证键、void_of、entry_type、business_date；
- InvoiceRecord/ReconciliationRun：与业务源记录建立唯一幂等关联。

数据库约束必须处理“先查后建”并发竞态；不能仅靠 Python 检查唯一性。

> 修订：2026-11-07 架构审查 —— 按 `635d9dd` 实证校准本节现状：①`LedgerEntry.business_date` 已存在（`apps/billing/models.py:263`，含 `(property, business_date)`、`(folio, business_date)` 索引），FIN-05 任务聚焦 Payment（`business_date`/`received_by` 确认缺失）与 CashierShift 的营业日口径对齐，LedgerEntry 侧为核对非新建；②收款/退款/账本/审批写权威现状已收口（Payment/LedgerEntry/Refund/ApprovalRecord 的 `.create()` 仅存在于 billing 服务层，前台收款经 front_desk_services 委托 BillingService），FIN-01/FIN-03/FIN-04 的主体是状态机扩展、SoD 与 DB 级不可变，非写入口新建；③存量跨域写漂移两项（审查报告 AR-04/AR-05：Folio 开关单由 reservations 流程直写）并入 SVC-05 收口后复检。

#### 服务/API/页面

- 服务：BillingService、RefundWorkflowService、ApprovalService、LedgerPostingService、ReconciliationService、CashierShiftService。
- API：账单、费用、收款、退款申请/审批/执行/查询、冲销、班次、对账、发票和财务报表。
- 页面：账单工作台、收银班次、退款审批队列、执行失败队列、对账异常队列、财务汇总。
- 财务人员、值班经理、店长等权限严格遵守正式权限基线；不临时扩大角色权限。

#### 验收

- 任何历史 LedgerEntry 不可被普通应用账号更新/删除；
- 退款必须区分批准和执行；
- 同一退款请求不能执行两次；
- 账务分录金额与来源业务一致；
- 失败的外部支付/退款有可恢复状态；
- Folio 转账两边凭证配对；
- 账务按营业日对账，不以 Payment 总和替代总账收入。

### 5.7 夜审与营业日中心（Night Audit & Business Date）

#### 功能需求（REQ 编号，不是执行任务 ID）

| 需求 ID | 优先级 | 功能需求 | 规则/完成条件 |
|---|---|---|---|
| REQ-AUD-01 | P0 | 营业日唯一打开规则 | 同物业同时最多一个 OPEN |
| REQ-AUD-02 | P0 | 夜审全量阻断项 | 预览收集所有 blockers，不遇首个错误即退出 |
| REQ-AUD-03 | P0 | 执行互斥与幂等 | 同物业同营业日不可并发重复执行 |
| REQ-AUD-04 | P1 | 夜审运行历史 | 每次运行状态、耗时、触发者、失败码、结果摘要 |
| REQ-AUD-05 | P1 | 营业日列表 API | 当前、历史、待关闭、异常状态 |
| REQ-AUD-06 | P1 | 失败恢复/重试 | 可重试错误与不可重试错误分类，支持人工恢复 |
| REQ-AUD-07 | P1 | 营业日重开 | 权限、原因、审批/复核、审计和后续重跑 |
| REQ-AUD-08 | P1 | 自动调度命令 | DB 锁、超时、重试、运行记录和告警 |
| REQ-AUD-09 | P2 | 夜审差异报告 | 账务、入住、库存和营业日核对 |

#### 字段评估

- BusinessDate：opened_by、closed_by、version、property timezone/cutoff 关联；
- NightAuditRun：error_code、started_at、finished_at、trigger_type、retry_of；
- NightAuditBlocker：如现有结构不足，记录 code、severity、object_type/id、message_key、resolved_at；
- BreakGlassRecord：核对现有字段和复核流程，不重复建模。

#### 服务/API/页面

- 服务：NightAuditService、BusinessDateService、NightAuditScheduler。
- API：营业日列表/详情、预览、执行、运行历史、失败详情、重试、重开申请/审批。
- 页面：营业日工作台、夜审预览、阻断项列表、运行历史、失败恢复面板。

#### 验收

- 同一物业同一时刻最多一个有效营业日；
- blocker 全量收集并按硬阻断/警告区分；
- 有硬阻断时执行拒绝；
- 并发执行不会重复过账；
- 失败可诊断、可恢复且保留运行历史；
- 物业时区和营业日截止规则一致。

### 5.8 清洁与维修中心（Housekeeping & Maintenance）

#### 功能需求（REQ 编号，不是执行任务 ID）

| 需求 ID | 优先级 | 功能需求 | 规则/完成条件 |
|---|---|---|---|
| REQ-HK-01 | P0 | 房间作业状态与可售状态分离 | 清洁状态不直接覆盖房间主状态 |
| REQ-HK-02 | P0 | 任务生成幂等 | 退房/维修事件重复触发不重复建任务 |
| REQ-HK-03 | P1 | 清洁任务分配/执行/检查 | PENDING→IN_PROGRESS→DONE→INSPECTED/REJECTED |
| REQ-HK-04 | P1 | 维修状态机 | OPEN→IN_PROGRESS→RESTORED/CANCELLED |
| REQ-HK-05 | P1 | 净房放行 | 指定角色/复核规则，审计原因 |
| REQ-HK-06 | P1 | 超时任务队列 | SLA、超时升级、转交和通知 |
| REQ-HK-07 | P1 | 房态库存联动 | 任务变化由服务统一计算可售性 |
| REQ-HK-08 | P2 | 维修成本/停房分析 | 有可靠来源后再纳入报表 |

#### 字段评估

- HousekeepingTask：assigned_to、priority、due_at、completed_at、inspected_by、rejected_reason、source_event_key；
- MaintenanceTicket：severity、reported_by、assigned_to、started_at、restored_at、out_of_service_from/to；
- RoomInspection：inspection_result、inspector、reason、checked_at。
- 若当前模型已具备字段则复用；不因方案重复加字段。

> 修订：2026-11-07 架构审查 —— 按 `635d9dd` 实证：`HousekeepingTask`/`MaintenanceTicket`/`RoomInspection` 三模型均已存在（`apps/inventory/models.py:295/355/399`）。本中心缺口是**任务池 API/UI 与流程**（HK-03/HK-05/HK-06：任务分配/执行/检查状态机、净房放行复核、超时队列），不是建模任务。A16 验收报告登记项"HousekeepingTask 任务池（§4.3 范围外）"据此并入本中心，原登记关闭。

#### 服务/API/页面

- 服务：HousekeepingService、MaintenanceService、RoomReleaseService、TaskAssignmentService。
- API：任务池、任务详情、分配、开始、完成、检查、驳回、维修登记/分配/恢复、超时队列。
- 页面：清洁任务池、楼层任务板、维修工单、净房检查、超时工作台。

#### 验收

- 退房事件可靠生成任务；
- 任务重复消费不重复创建；
- 清洁完成未检查时不能错误释放房间；
- 维修恢复不自动绕过净房检查；
- 每次状态变化有操作者和时间；
- 超时、转交和失败重试可追踪。

### 5.9 审计与合规中心（Audit & Compliance）

#### 功能需求（REQ 编号，不是执行任务 ID）

| 需求 ID | 优先级 | 功能需求 | 规则/完成条件 |
|---|---|---|---|
| REQ-CMP-01 | P0 | AuditEvent 数据库级不可变 | 应用层和 DB 层双重保护 |
| REQ-CMP-02 | P0 | 审计 PII 最小化 | 不记录完整 query string、证件明文、token、密码 |
| REQ-CMP-03 | P1 | 事件覆盖率矩阵 | 关键写操作 100% 有审计 |
| REQ-CMP-04 | P1 | 审计检索/详情权限 | 独立权限、物业范围、导出控制 |
| REQ-CMP-05 | P1 | 留存与归档策略 | 按数据类别保留、归档、删除/匿名化 |
| REQ-CMP-06 | P1 | 完整性校验 | hash/checkpoint/归档校验和和恢复演练 |
| REQ-CMP-07 | P2 | 合规操作报告 | 高风险揭示、导出、Break-glass、审批复核 |

#### 技术要求

- AuditEvent 不允许业务账号 UPDATE/DELETE；
- MySQL 账号权限、触发器或受控写入通道形成数据库级防护；
- 审计事件包括 actor、property、action、object、reason、request_id、timestamp、result；
- 审计查询本身避免无限递归记录，采用专门安全事件策略；
- 日志和审计分离：日志排障，审计证明业务行为；
- 任何导出不得默认输出完整敏感字段。

### 5.10 报表分析中心（Reporting & Analytics）

#### 功能需求（REQ 编号，不是执行任务 ID）

| 需求 ID | 优先级 | 功能需求 | 规则/完成条件 |
|---|---|---|---|
| REQ-RPT-01 | P0 | 指标口径注册表 | 每个指标定义公式、数据源、时区、过滤条件和版本 |
| REQ-RPT-02 | P0 | 财务指标权威数据源 | 收入以 LedgerEntry/批准口径，不直接累计 Payment |
| REQ-RPT-03 | P1 | 经营日报/月报 | 入住率、ADR、RevPAR 等指标定义与分母一致 |
| REQ-RPT-04 | P1 | 预订渠道/取消/No-show 分析 | 使用稳定来源码和事件时间 |
| REQ-RPT-05 | P1 | 房态清洁维修分析 | 任务耗时、停房、清洁时长和房间可售率 |
| REQ-RPT-06 | P1 | 报表权限与物业范围 | 角色、数据范围、敏感字段和导出策略 |
| REQ-RPT-07 | P2 | 异步大报表 | 任务状态、下载权限、过期清理 |
| REQ-RPT-08 | P2 | 指标版本和历史重算 | 公式变化不静默改变历史口径 |

#### 指标治理

每个指标需定义：
- code/name/description；
- numerator/denominator；
- source model/field；
- event date/business date；
- property timezone；
- excluded statuses；
- currency/tax treatment；
- rounding；
- version/effective date；
- permission/export policy；
- test fixture and expected values。

报表必须有固定数据集的黄金样本，结果与 SQL/账务事实对账。复杂报表使用预聚合或缓存前必须证明口径正确。

### 5.11 通知与待办中心（Work Items & Notifications）

#### 功能需求（REQ 编号，不是执行任务 ID）

| 需求 ID | 优先级 | 功能需求 | 规则/完成条件 |
|---|---|---|---|
| REQ-WN-01 | P0 | 待办源事件去重 | dedupe_key 唯一，事件重复不重复生成 |
| REQ-WN-02 | P0 | 源业务状态校验 | 待办完成前重新校验源对象状态和权限 |
| REQ-WN-03 | P1 | 分配/转交/处理/关闭 | 状态机、处理人、原因和审计 |
| REQ-WN-04 | P1 | 通知投递可靠性 | outbox/重试/失败队列/幂等 |
| REQ-WN-05 | P1 | 离职员工待办转交 | 主管接管或队列回收，不丢任务 |
| REQ-WN-06 | P1 | 通知偏好与静默 | 不影响安全/财务强制通知 |
| REQ-WN-07 | P2 | 站内通知中心 | 未读、已读、跳转源业务对象 |
| REQ-WN-08 | P2 | 运营待办 SLA | 超时、升级、统计 |

通知是投递副本，WorkItem 是待处理任务，源业务对象仍是业务事实权威。禁止通过“标记通知已读”直接改变预订/退款/维修源状态。

> 修订：2026-11-07 架构审查 —— 按 `635d9dd` 实证校准本节现状：①`Notification`/`NotificationRead` 模型与 `dedupe_key`（property+dedupe_key 数据库唯一）已在位，`apps/core/services_notifications.py` 已实现实时重算+upsert 对账的待办派生，WN-01 的去重机制已落地；②存量漂移两项随本中心任务包清偿：派生服务对 billing/identity/inventory/reservations 的模块级反向依赖（审查报告 AR-02，违反 §3.2/§3.4，重构为独立 notifications app 或业务侧"待办提供者"注册）；API 层直写 `NotificationRead`（AR-08，收口为服务方法并入 WN-03）。

---

## 6. 跨中心数据流与业务闭环

### 6.1 核心数据流

1. 员工入职 → User/StaffProfile → PropertyStaffMembership → RoleAssignment → AuthorizationService → 审计。
2. Guest 创建/复核 → GuestPhone/GuestDocument → ReservationGuest → StayGuest → Guest Timeline。
3. Room/RoomType → RatePlan/DailyRate → InventoryAvailability → InventoryHold → Reservation。
4. Reservation 确认 → 押金/收款 → Stay → Folio → ChargeLine/Payment/LedgerEntry。
5. Stay 退房 → 结账 → HousekeepingTask → RoomInspection → RoomStatus/Inventory。
6. MaintenanceTicket → OutOfService → InventoryAvailability → 修复/检查 → 恢复可售。
7. Refund Request → Approval → Execution → LedgerEntry → AuditEvent → Notification/WorkItem。
8. BusinessDate → NightAudit Preview → Blockers → Execution → LedgerEntry/Run History → Report。
9. 关键业务事件 → Outbox/WorkItem/Notification → 处理结果回写源业务服务。
10. 领域事件/账务事实 → 指标服务 → 报表/导出。

### 6.2 端到端场景强制清单

| 场景 | 必须验证的模块 | 关键结果 |
|---|---|---|
| 正常预订入住退房 | Guest、Inventory、Reservation、Stay、Billing、Housekeeping | 库存、人员、账单、清洁和房态一致 |
| 多房预订部分入住 | Reservation、Stay、Inventory、Billing | 每间房和入住人独立履约 |
| 取消并退款 | Reservation、Approval、Billing、Audit、Notification | 政策快照、罚金、退款和账本一致 |
| 并发预订 | Inventory、Reservation、Idempotency | 不超卖、不重复订单 |
| 员工离职 | Identity、Authorization、Session、Audit、WorkItem | 会话撤销、权限失效、待办转交 |
| Guest 合并 | Guest、Membership、Reservation、Stay、Billing | 关系迁移可追溯，不丢历史 |
| 维修停房 | Maintenance、Room、Inventory、Reservation | 不可售房间不会被继续预订 |
| 夜审失败恢复 | Billing、BusinessDate、NightAudit、Audit | 不重复过账，可恢复 |
| 退款审批分离 | Billing、Approval、Identity | 申请、审批、执行责任清晰 |
| 大报表导出 | Report、Authorization、Audit、Storage | 权限、限流、异步、过期清理 |
| 通知重复消费 | Core、Outbox、Notification、WorkItem | 去重且源业务只执行一次 |

---

## 7. API 与前后端统一规范

### 7.1 API 版本和契约

- 所有 PMS 内部 API 统一 `/api/v1/`；破坏性契约升级使用新版本，不静默改变字段含义。
- 成功响应统一 `{data, meta}`；错误响应统一 `{error, meta}`。
- 每个响应包含 request_id；列表包含 count/next/previous 或 cursor meta。
- 日期用 ISO 8601，明确 timezone；纯日期用 YYYY-MM-DD。
- 金额使用十进制字符串，明确 currency；禁止 JS 浮点金额。
- 状态返回稳定 code 和展示 label；前端不得依赖中文 label 判断业务。
- API 只返回 DTO，不直接序列化模型全部字段。
- 物业上下文由服务端身份/任职解析，禁止相信客户端 property_id。
- 写操作执行认证、授权、CSRF、参数校验、事务、幂等和审计。

### 7.2 错误码目录

建立统一错误码注册表，至少包含：

- AUTHENTICATION_REQUIRED；
- PERMISSION_DENIED；
- PROPERTY_SCOPE_DENIED；
- VALIDATION_ERROR；
- RESOURCE_NOT_FOUND；
- STATE_TRANSITION_INVALID；
- IDEMPOTENCY_CONFLICT；
- INVENTORY_CONFLICT；
- APPROVAL_REQUIRED；
- APPROVAL_SEPARATION_VIOLATION；
- BUSINESS_DATE_CLOSED；
- RATE_LIMITED；
- DEPENDENCY_UNAVAILABLE；
- INTERNAL_ERROR。

错误信息不得泄露 SQL、文件路径、密钥、PII 或内部堆栈。

### 7.3 分页与排序

- 普通后台列表默认 offset 分页，最大 page_size 受限；
- 高增长审计/流水/通知可使用 cursor；
- cursor 模式不得同时接受 page>1；
- 所有分页排序必须稳定，末尾追加唯一键；
- 日期筛选采用左闭右开；
- 财务、审计和导出接口必须限制日期跨度和最大记录数；
- 排序字段使用白名单，不能直接拼接用户输入 SQL。

### 7.4 OpenAPI 与 Apifox

每个 API 变更必须同步：
- OpenAPI operation；
- 请求/响应 schema；
- 错误码；
- 认证/权限说明；
- 示例和分页说明；
- Apifox 集合/环境；
- 自动化 API 测试；
- 变更日志。

CI 必须检查 OpenAPI 与实际路由/契约漂移；Apifox 实际执行结果需归档。

> 修订：2026-11-07 架构审查 —— Apifox 交付为 2.0 遗留缺口：仓库现有 `docs/接口文档/openapi.json`（97 paths）与 `apifox-env-template.json` 模板，但**实际导入与执行报告缺失**（A16 证据包未含）。API-07 为其闭环任务；在完成前，2.0 验收标准 §13"Apifox 实际导入和执行报告作为独立证据"的口径对 3.0 同样适用。

### 7.5 前端规范

- 统一导航、页面宽度、间距、字体、按钮、表格、表单、弹窗、抽屉、状态标签；
- 员工中心最终 UI 规范作为基础参考，其他中心按业务复杂度适配，不机械复制；
- 列表页统一筛选、排序、分页、空态、加载态、错误态；
- 详情页使用稳定 URL，所有“查看”必须真实可达；
- 写操作有二次确认、原因输入、权限提示和成功/失败反馈；
- 高风险动作不能仅靠颜色或图标表达；
- 表单服务端错误需映射到字段；
- 详情抽屉关闭/刷新后状态一致；
- 页面不得使用 Mock 数据伪装真实数据；
- 桌面优先，同时支持常见窄屏，不把手机端作为唯一业务工作台；
- 关键页面有桌面和窄屏 E2E 截图证据。

---

## 8. 身份、权限、会话与安全

### 8.1 身份模型

- 员工账号由管理员创建/邀请，不开放 PMS 权限自注册；
- 员工用户名是当前主登录标识；未验证手机号/邮箱不得作为登录别名；
- User、StaffProfile、PropertyStaffMembership、StaffRoleAssignment 分离；
- 账号激活、任职有效和岗位授权有效分别判定；
- 员工离职/暂停时撤销会话与授权；
- 不默认引入员工自助找回密码、MFA 或顾客 PMS 账号，需独立决策。

### 8.2 权限模型

- AuthorizationService 是唯一授权判断入口；
- 权限码注册表是唯一权限标识；
- 九个正式角色沿用冻结基线，新增角色/权限需负责人批准；
- 角色授权需有物业范围、生效时间、原因和操作者；
- 高风险动作实施职责分离；
- UI 隐藏不构成授权；
- 对象级授权防止水平越权；
- 每个请求重新校验有效任职和权限，不长期缓存不可撤销的权限结果；
- Admin、Command、API、页面和后台任务遵守同一授权策略。

### 8.3 敏感字段

- 身份证件加密；密钥与 SECRET_KEY 分离；
- 采用带版本号的密钥管理，支持轮换、双版本解密和迁移；
- 证件完整揭示有独立权限、原因、限流和审计；
- 手机号按角色遮罩，完整揭示按既有决策；
- 工资和财务字段遵守最小权限；
- 日志不记录明文 PII、token、密码、cookie、完整 query string；
- 导出权限独立于页面查看权限。

### 8.4 会话和 Cookie

继续遵循已确认的平台/业务 Session-Cookie 隔离方案；任何改动必须回归：
- 员工 PMS 与平台后台 Cookie Path/Name/Domain 隔离；
- 登录、登出、CSRF、强制改密；
- 多标签页、会话过期、权限撤销；
- HTTPS Secure、HttpOnly、SameSite；
- 可信代理和 Host 校验；
- 不安全的 next redirect 必须拒绝外域和协议相对地址。

### 8.5 安全测试

必须覆盖 OWASP Top 10 相关风险，重点包括：
- 未认证访问；
- 水平/垂直越权；
- CSRF、XSS、SQL 注入；
- SSRF（若有外部 URL 获取）；
- 文件上传和导出；
- 会话固定/盗用；
- PII 枚举；
- 限流绕过；
- 业务逻辑重放；
- 审批职责分离；
- 依赖漏洞和密钥泄漏。

---

## 9. Redis、缓存与后台任务

### 9.1 Redis 使用边界

Redis 不是数据库权威，也不能代替 MySQL 事务。只在确有跨进程共享需求时使用：

| 场景 | Redis 用途 | 权威数据 |
|---|---|---|
| Django Cache | 高频只读数据缓存 | MySQL |
| Rate Limit | 跨 worker 计数/窗口 | 策略与必要审计仍在应用/DB |
| 临时幂等协调 | 降低重复请求竞争 | MySQL 唯一约束/幂等记录 |
| 短时分布式锁 | 调度互斥辅助 | DB 状态/唯一约束 |
| 临时验证码/邀请防重 | TTL 临时状态 | DB 邀请记录 |
| 报表短期缓存 | 查询结果缓存 | 报表数据源 |

禁止将账务、预订、库存、员工身份、权限授权、审计等唯一事实只存 Redis。

### 9.2 缓存规则

- 每类缓存定义 key namespace、TTL、失效事件和最大内存；
- 缓存值不包含不应共享的用户敏感数据；
- 权限和员工任职变化主动失效；
- Redis 不可用时，按风险选择 fail-closed 或受控降级；
- 限流不可在 Redis 故障时无保护地放开敏感接口；
- 缓存击穿/雪崩有保护；
- 缓存命中不能绕过服务端授权。

### 9.3 后台任务

可先采用 Django Management Command + 数据库任务表/锁，不默认引入 Celery。每个任务必须定义：
- 唯一任务类型和业务键；
- 调度频率/触发事件；
- 幂等键；
- 锁与超时；
- 重试次数和退避；
- 可重试/不可重试错误；
- 执行记录、结果摘要和告警；
- 人工重放/取消规则。

重点任务：
- 邀请过期；
- Reservation Hold 过期；
- No-show 处理；
- 夜审调度；
- 清洁/维修 SLA；
- 通知投递；
- 报表生成和文件过期清理；
- GuestDocument retention 清理；
- 审计归档和完整性校验；
- RateLimitCounter/幂等记录清理；
- Django session 清理。

每个命令必须具备 dry-run（适用时）、互斥锁、物业范围、审计和运行报告。

---

## 10. 数据迁移与旧系统退役

### 10.1 迁移流程

所有迁移必须采用：

1. Inventory：盘点源表、字段、引用和数据质量；
2. Backup：数据库与文件附件备份并恢复演练；
3. Expand：新增兼容字段/表/索引；
4. Backfill：幂等、分批、可断点续跑；
5. Verify：记录数、金额、状态、关联、抽样明细对账；
6. Cutover：冻结旧写入，切换唯一写服务；
7. Observe：只读观察期，监控差异；
8. Archive：历史归档、校验和、来源映射；
9. Contract：独立删除废弃路由、字段、表和代码。

### 10.2 LegacyRecordLink

每条迁移记录必须有：
- source_system/source_table/source_pk；
- target_model/target_pk；
- mapping_status；
- mapping_rule_version；
- migrated_at；
- checksum；
- error_code/error_summary；
- reviewed_by/reviewed_at（人工复核时）。

唯一键保证同一来源记录不会重复映射。冲突不得静默覆盖。

> 修订：2026-11-07 架构审查 —— `LegacyRecordLink` 模型已存在于 `apps/core/models.py:135`。本节任务为按上述字段清单核对既有字段完整性并落地真实迁移映射（DB-16/MIG-04），非新建模型。

### 10.3 退役矩阵

逐项列出：
- 旧 URL/HTTP Method；
- 旧页面/模板；
- 旧 View/Service；
- 旧模型/字段；
- 调用方/外部集成；
- 新入口；
- 退役状态（冻结写入/只读/归档/410/删除）；
- 数据迁移状态；
- 测试与负责人。

只有完成业务覆盖、数据对账、归档和回滚准备后才允许删除。账务、审计、审批、订单和历史员工记录不得因版本升级被直接删除。

> 修订：2026-11-07 架构审查 —— 退役矩阵首轮实证（基线 `635d9dd`）：①35 条旧写路由已 410 封死且有 `apps/identity/test_module_gates.py`（RETIRED_410_ROUTES）护栏；②**例外**：`/no_show_one/`（`hotelapp/views.py:1357`）为活跃旧写入口，直写旧 Accommodation/Room 并触发旧事件处理器，未在 410 清单内（审查报告 AR-01）——MIG-06 冻结旧写路径时必须显式包含，业务确认见 DEC-24；③`hotelapp/audit_middleware.py`（请求级写操作日志）为在役基础设施组件，位于遗留包内——Contract 阶段删除 hotelapp 前必须先迁移该中间件；④`hotelapp/services/*` 为无调用方死代码，`folio_api` 等活跃只读视图仍读旧表，归档前保持只读。

---

## 11. 测试策略与质量门槛

### 11.1 测试分层

| 层级 | 覆盖范围 |
|---|---|
| Unit | 领域规则、状态机、金额、日期、权限策略 |
| Service | 事务、回滚、幂等、并发、异常恢复 |
| Model/DB | 约束、索引、迁移、MySQL 锁和隔离级别 |
| API | 契约、认证、授权、错误码、分页、限流 |
| Integration | 跨模块事件和数据流 |
| E2E | 核心用户工作流真实浏览器操作 |
| Security | 越权、PII、导出、会话、审计 |
| Performance | API、列表、报表、并发交易 |
| Resilience | Redis/DB/任务故障、重试和恢复 |
| Migration | 空库、升级库、回填、回滚/恢复 |

### 11.2 强制质量门槛

- P0/P1/P2 缺陷未关闭数量为 0；
- 核心端到端链路通过率 100%；
- 九角色权限强制用例通过率 100%；
- MySQL 8 迁移、约束、并发用例通过率 100%；
- OpenAPI 契约漂移检查通过；
- 强制测试不得有未解释 skip/xfail；
- 不得为了通过 CI 删除测试或弱化断言；
- 核心服务建议行覆盖率 ≥90%，但覆盖率不能替代规则分支测试；
- 每个新增状态必须覆盖合法/非法迁移；
- 每个写 API 必须覆盖重复请求和并发；
- 每个敏感字段必须覆盖授权与未授权路径；
- 每个后台任务必须覆盖重复执行和失败恢复。

### 11.3 数据库矩阵

CI 至少包含：
- SQLite 快速测试；
- MySQL 8 全量测试；
- MySQL 8 空库迁移；
- MySQL 8 从上一版本升级；
- `makemigrations --check --dry-run`；
- 关键并发与事务测试。

SQLite 不能作为 MySQL 的替代验证。

### 11.4 E2E 核心脚本

至少自动化：
- 九角色登录/登出和权限边界；
- 员工邀请、激活、调岗、离职；
- Guest 创建、共享手机号复核、证件揭示；
- 房态、价格、库存、并发预订；
- 预订、入住、换房、续住、退房；
- 退房后清洁、检查、维修和重新可售；
- 退款申请、审批、执行、失败重试；
- 夜审预览、阻断、执行和失败恢复；
- 报表筛选、导出和越权拒绝；
- 待办分配、转交、处理和通知去重。

---

## 12. 性能、可观测性和可靠性

### 12.1 性能基线

性能目标不能脱离部署资源和数据规模。启动压测前冻结：
- CPU/内存/磁盘/网络；
- Python/Django/WSGI/MySQL/Redis 版本；
- 表数据量和热点分布；
- 并发用户数、请求比例、运行时长；
- 连接池、缓存和 worker 数；
- P50/P95/P99、吞吐、错误率和资源指标。

建议初始目标供负责人冻结：
- 普通查询 API P95 ≤ 500ms；
- 普通写 API P95 ≤ 800ms；
- 复杂报表 P95 ≤ 3s 或改为异步；
- 核心写事务不允许重复写入和数据丢失；
- 关键列表无 N+1 查询，分页查询有执行计划；
- 高峰期间错误率、连接池和资源占用在容量目标内。

### 12.2 可观测性

统一记录：
- request_id / correlation_id；
- actor_id、property_id（非敏感内部标识）；
- route、method、status、duration；
- service/operation；
- error_code；
- DB query timing/slow query；
- task_id、retry_count；
- audit_event_id（适用时）。

禁止记录密码、token、完整 Cookie、证件明文和完整敏感 query string。

### 12.3 健康检查

- liveness：进程是否存活；
- readiness：MySQL、Redis（按强依赖配置）、迁移和必要服务是否可用；
- dependency status：依赖异常要有分类；
- 不在公开健康接口泄露连接串、主机内部路径或异常堆栈。

### 12.4 备份与灾难恢复

必须定义 RPO/RTO，覆盖：
- MySQL 全量/增量/binlog；
- 上传附件和证件密文；
- 密钥备份和轮换恢复；
- Redis 是否需要持久化（按其非权威用途决定）；
- 备份加密、异地存放、访问控制和保留周期；
- 恢复演练、数据校验、业务抽样和演练报告。

---

## 13. 前端信息架构与 UI 交付

### 13.1 一级导航建议

一级导航按实际运营岗位组织，避免技术模块名和重复入口：

1. 首页/工作台；
2. 预订与入住；
3. 房态与库存；
4. Guest 360；
5. 会员；
6. 清洁与维修；
7. 财务与账单；
8. 夜审与营业日；
9. 报表分析；
10. 通知与待办；
11. 员工中心；
12. 审计与合规；
13. 权限管理（独立管理域，非员工档案入口）；
14. 系统设置（仅平台授权人员）。

菜单按角色和物业范围展示，但服务端仍须独立授权。

### 13.2 页面模板规范

每个中心按其业务类型设计，不允许一个通用表格模板承载所有操作。统一：
- 页面标题、面包屑、主操作；
- 筛选区、结果表、分页；
- 状态标签和空/错/加载态；
- 详情页或详情抽屉；
- 操作确认和原因输入；
- 权限不足提示；
- Request ID/错误码支持排障；
- 响应式布局和键盘可访问性。

### 13.3 页面交付清单

每个页面必须提交：
- 路由与权限；
- 页面截图（实际运行版本）；
- API 调用清单；
- 字段显示/遮罩说明；
- 按钮动作和状态变化；
- 加载/空态/错误态；
- 角色验收；
- E2E 测试。

---

## 14. 开发阶段、依赖关系与排期建议

所有阶段均以 PMS2 最终验收通过为正式开发前置条件。阶段可并行设计，但代码合并按依赖控制。

### 14.0 任务包前置依赖与验收门槛（2026-11-07 架构审查修订）

> 修订：2026-11-07 架构审查 —— 按审查报告（`docs/audit/phase0/80-architecture-review.md`）补充各 Phase 的前置依赖与验收门槛，门槛风格对齐 PMS 2.0 验收标准 §18 总闸门（G 项）；"待负责人确认"事项单列，不混入开发任务。

| Phase | 前置依赖 | 退出/验收门槛（对齐 2.0 验收标准风格） | 待负责人确认 |
|---|---|---|---|
| Phase 0 基线复核 | PMS2 最终验收“通过”（G14 签署）；PM-02 基线冻结 | BASE-01~10 盘点产物齐备且可复现；审查报告 AR 登记项全部有归属任务或决策编号 | 无 |
| Phase 1 领域模型与数据库 | Phase 0；DB-01 字段字典先行 | MySQL 8 迁移可执行、无 makemigrations 漂移、约束/索引证据齐（对齐 2.0 G04） | DEC-01~05、DEC-08/09（阻塞对应 DB 任务） |
| Phase 2 核心领域服务 | Phase 1 中对应实体建模完成；AR-03/04/05/06 收口随 SVC-02/04/05 执行 | 写路径 100% 收口服务（复检审查报告 §2.1 六实体矩阵）；状态机/幂等/并发/审计测试通过 | DEC-07、DEC-10、DEC-13~15 |
| Phase 3 API 契约与安全 | Phase 2 对应服务在位；`core/api_contract.py` 统一封套已推广完毕 | 旧写入口为 0（含 `/no_show_one/`，G11）；OpenAPI 无漂移；负向安全用例 100%（对齐 2.0 G05/G07/G11） | DEC-23（审计读取策略） |
| Phase 4 前端工作台 | 冻结的 API 契约（Phase 3 并行约束）；无 Mock 假数据 | 11 中心页面真实接 API、详情路由可达、桌面/窄屏 E2E 证据（对齐 2.0 G02/G12） | 无 |
| Phase 5 迁移与退役 | Phase 1/2 完成；DEC-24 旧工作台依赖确认 | 无双写双算、410 全量（路由表全量 diff 权威清单，含 AR-01 补录）、LegacyRecordLink 对账通过 | DEC-24 |
| Phase 6 测试/性能/韧性 | 各功能 Phase 完成度 ≥ 申报可测 | 强制测试 100%、性能达冻结目标、备份恢复演练通过（对齐 2.0 G08/G10） | DEC-21（容量目标冻结） |
| Phase 7 上线与独立验收 | Phase 0~6 全部退出 | 证据包 A01-A16 完整、独立验收签署（对齐 2.0 G13/G14） | 无 |

阶段内依赖补充：SVC-10（统一幂等/Outbox/Retry）先于 WN-04 与 FIN-04 联调；API-08（旧写入口收口）先于 MIG-06；PM-05（依赖规则 CI）在 Phase 2 首个合入前就位，基线即审查报告 §1.4 依赖矩阵。

### Phase 0：启动门禁与基线复核（P0）

执行任务：GATE-01~GATE-07、PM-01~PM-04。
交付：最新 main SHA、环境/依赖清单、11 个中心现状矩阵、模型/字段/写入口/API/页面/权限/测试映射、未决决策清单、备份恢复证据。
退出标准：PMS2 最终验收通过；基线可复现；所有 REQ 需求有任务归属、依赖和验收证据定义。

### Phase 1：领域模型与数据库治理（P0）

执行任务：DB-01~DB-20，并完成 BIZ-* 任务中标记为 Phase 1 的数据模型/字段/约束子项。
退出标准：字段字典完整；无重复权威实体；MySQL 8 空库迁移、升级迁移、约束、索引、数据回填和对账证据通过；不修改已发布 migration。

### Phase 2：核心领域服务和状态机（P0/P1）

执行任务：SVC-01~SVC-10，并完成 BIZ-* 任务中标记为 Phase 2 的领域服务子项。
退出标准：关键写路径 100% 收口到唯一服务；状态机、事务、幂等、并发、补偿和审计测试通过；跨 App 依赖方向无新增违例。

### Phase 3：API 契约与安全治理（P0/P1）

执行任务：API-01~API-10，并完成 BIZ-* 任务中标记为 Phase 3 的 API 子项。
退出标准：每个写 API 可追踪到服务、权限和审计；OpenAPI 与真实路由/DTO 无漂移；对象/物业越权、CSRF、限流、Token 泄露和重放负向用例通过；旧写入口退役以 MIG-06 为最终门禁。

### Phase 4：前端工作台与跨中心闭环（P1）

执行任务：UI-01~UI-04，并完成所有 BIZ-* 任务的页面、表单、按钮和真实 API 接线。
交付顺序：预订入住、房态库存、Guest 360、清洁维修、财务账单、夜审营业日、员工、会员、通知待办、报表、审计。
退出标准：无 Mock 假数据；所有页面路由可达；按钮状态、异常态、权限态和表单校验正确；11 个中心的页面/API/服务/权限逐项对应。

### Phase 5：迁移、旧入口退役与数据对账（P0/P1）

执行任务：MIG-01~MIG-10。
退出标准：迁移可重复、可断点续跑、可核验；无双写双算；LegacyRecordLink 来源映射完整；35 条既有 410 路由与 /no_show_one/ 等全部旧写入口均有全量路由 diff 和负向测试；归档后才允许 Contract。

### Phase 6：自动化测试、性能和韧性（P0/P1/P2）

执行任务：QA-01~QA-10。
退出标准：强制测试 100% 通过；MySQL 8 与 SQLite 结果差异可解释；多 worker、Redis/DB 故障、回调重复/乱序、后台任务中断、备份恢复、库存并发、财务对账均有可复现证据。

### Phase 7：上线准备与独立验收（P0）

执行任务：REL-01~REL-09。
退出标准：生产配置、发布/回滚、运维手册、培训、监控告警、RPO/RTO 演练、证据包和独立验收签署齐备；只有最终结论“通过”才允许发布。

## 15. 全项目任务清单（V2 唯一执行任务台账）

### 15.1 业务需求执行台账

§5 的 REQ-* 是业务需求编号；以下 BIZ-* 是唯一业务执行任务编号。两者一一映射。所有 issue、commit、测试报告和验收记录必须使用 BIZ-* 作为任务 ID，并同时引用来源 REQ-*。需求编号不得直接冒充工单编号。

| 执行任务 ID | 来源需求 ID | 优先级 | 任务名称 | 验收重点 |
|---|---|---|---|---|
| BIZ-EMP-01 | REQ-EMP-01 | P0 | 员工身份/档案/任职/授权模型核对 | 明确 User、StaffProfile、Membership、Assignment 关系 |
| BIZ-EMP-02 | REQ-EMP-02 | P0 | 员工编号规则 | 物业范围唯一；正式工号不可随意修改；离职工号保留且不可复用 |
| BIZ-EMP-03 | REQ-EMP-03 | P0 | 再入职模型 | 决策“恢复原任职”还是“新建任职记录”；不得被简单唯一约束阻断 |
| BIZ-EMP-04 | REQ-EMP-04 | P1 | 入职/邀请/激活 | 管理员创建邀请；一次性 token、过期、撤销、使用审计 |
| BIZ-EMP-05 | REQ-EMP-05 | P1 | 调岗/换部门/角色变更 | 记录生效时间、原因、操作者、原值和新值 |
| BIZ-EMP-06 | REQ-EMP-06 | P1 | 暂停/离职/恢复 | 立即撤销有效会话和岗位授权；保留历史 |
| BIZ-EMP-07 | REQ-EMP-07 | P1 | 员工详情时间线 | 任职、角色、账号、密码重置和状态历史 |
| BIZ-EMP-08 | REQ-EMP-08 | P1 | 员工批量导入/导出 | 仅批准字段；预览、校验、冲突报告、权限和审计 |
| BIZ-EMP-09 | REQ-EMP-09 | P2 | 员工数据质量盘点 | 未绑定 User、无 Membership、重复工号和失效授权报表 |
| BIZ-EMP-10 | REQ-EMP-10 | P2 | 账号安全操作 | 管理员重置密码、强制改密、禁用、会话撤销 |
| BIZ-GST-01 | REQ-GST-01 | P0 | Guest 唯一主档与重复识别策略 | 手机号不能单独证明自然人唯一 |
| BIZ-GST-02 | REQ-GST-02 | P0 | 联系方式可信度/共享手机号 | 共享号码人工复核、primary_guest_key 规则明确 |
| BIZ-GST-03 | REQ-GST-03 | P0 | 证件密钥版本与轮换 | 密钥版本可识别，轮换可恢复，禁止无密钥解密 |
| BIZ-GST-04 | REQ-GST-04 | P1 | Guest 合并工作流 | 提案、审核、合并、撤销/补偿、关联迁移和审计 |
| BIZ-GST-05 | REQ-GST-05 | P1 | 实际入住人关系 | ReservationGuest、StayGuest、StayRoomSegment 真实关联 |
| BIZ-GST-06 | REQ-GST-06 | P1 | Guest 时间线 | 预订、入住、房间段、账单、会员、投诉/服务事件 |
| BIZ-GST-07 | REQ-GST-07 | P1 | 客人偏好与联系授权 | 仅纳入有明确用途的字段，记录来源和同意版本 |
| BIZ-GST-08 | REQ-GST-08 | P1 | 证件留存和到期清理 | retention_until、法律保留例外、删除任务审计 |
| BIZ-GST-09 | REQ-GST-09 | P2 | Guest 数据质量 | 重复、缺字段、共享手机号、失效证件统计 |
| BIZ-GST-10 | REQ-GST-10 | P2 | 客人档案导出 | 最小化、审批/理由、字段脱敏、审计 |
| BIZ-INV-01 | REQ-INV-01 | P0 | 可售库存唯一计算规则 | 逐日房型库存、已售、冻结、停售、维修和占用一致 |
| BIZ-INV-02 | REQ-INV-02 | P0 | 并发库存锁与版本控制 | 乐观版本/行锁/唯一约束组合，不能超卖 |
| BIZ-INV-03 | REQ-INV-03 | P0 | InventoryHold 生命周期 | 创建、确认、释放、过期、清理、重试可追溯 |
| BIZ-INV-04 | REQ-INV-04 | P1 | 房态工作台 | 房间、房型、楼层、清洁、维修和入住状态视图 |
| BIZ-INV-05 | REQ-INV-05 | P1 | 维修/清洁影响库存 | 状态变化通过服务同步，不直接改库存计数 |
| BIZ-INV-06 | REQ-INV-06 | P1 | 价格日历和 RatePlan | 有效期、币种、税费、关闭日、版本历史 |
| BIZ-INV-07 | REQ-INV-07 | P1 | 房间自然排序 | 楼栋/楼层/房号排序规则，不能纯字符串排序 |
| BIZ-INV-08 | REQ-INV-08 | P1 | 批量房态操作 | 预览影响、权限、理由、逐项结果和审计 |
| BIZ-INV-09 | REQ-INV-09 | P2 | 库存差异监控 | 每日核对 reservation/hold/room inventory |
| BIZ-INV-10 | REQ-INV-10 | P2 | 价格变更审计 | 记录旧值、新值、原因、生效时间和操作者 |
| BIZ-MEM-01 | REQ-MEM-01 | P0 | Guest 与会员关系唯一性 | 一位 Guest 在同一会员体系下的关系规则明确 |
| BIZ-MEM-02 | REQ-MEM-02 | P1 | 会员等级变更服务 | 权限、原因、生效时间和历史不可覆盖 |
| BIZ-MEM-03 | REQ-MEM-03 | P1 | 暂停/恢复/注销 | 状态机和重新激活规则 |
| BIZ-MEM-04 | REQ-MEM-04 | P1 | 会员历史与权益摘要 | 只展示已批准范围内真实权益 |
| BIZ-MEM-05 | REQ-MEM-05 | P2 | 会员标签/偏好 | 仅基于合法来源和业务用途 |
| BIZ-MEM-06 | REQ-MEM-06 | P2 | 会员报表 | 会员数量、等级变动和活跃口径冻结 |
| BIZ-RSV-01 | REQ-RSV-01 | P0 | Reservation/Stay 状态机统一 | 状态定义、合法迁移、终态规则明确 |
| BIZ-RSV-02 | REQ-RSV-02 | P0 | 预订并发与幂等 | 公共/后台/前台入口统一服务，防重复和超卖 |
| BIZ-RSV-03 | REQ-RSV-03 | P0 | 取消政策快照 | 创建时保存适用政策版本、计算结果和时点 |
| BIZ-RSV-04 | REQ-RSV-04 | P0 | 押金政策版本与事实快照 | 预订确认时保存适用政策和金额规则 |
| BIZ-RSV-05 | REQ-RSV-05 | P1 | 多房预订履约 | 部分入住、部分退房、换房和取消独立处理 |
| BIZ-RSV-06 | REQ-RSV-06 | P1 | Walk-in 业务决策 | 明确是否支持无预订直接入住；若支持，建立合法来源和账务链；若不支持，明确拒绝入口 |
| BIZ-RSV-07 | REQ-RSV-07 | P1 | No-show 与过期释放统一 | 合并重复命令逻辑，单一调度入口和锁 |
| BIZ-RSV-08 | REQ-RSV-08 | P1 | 延住/换房/提前离店 | 重新核验库存、价格、押金、账单和房间段 |
| BIZ-RSV-09 | REQ-RSV-09 | P1 | 预订变更历史 | 保存旧值、新值、原因、操作者和政策影响 |
| BIZ-RSV-10 | REQ-RSV-10 | P0 | 公共预订安全与生命周期 | 访问令牌、速率限制、库存占用和订单查询保护 |
| BIZ-RSV-11 | REQ-RSV-11 | P2 | 预订来源统计 | 来源代码、渠道、活动等口径冻结 |
| BIZ-RSV-12 | REQ-RSV-12 | P2 | 订单/入住异常队列 | 超期、状态冲突、缺少入住人、房间段断链 |
| BIZ-RSV-13 | REQ-RSV-13 | P0 | 支付渠道适配与环境隔离 | 定义 provider adapter；生产环境拒绝 mock；密钥独立管理；渠道/币种/商户配置可审计 |
| BIZ-RSV-14 | REQ-RSV-14 | P0 | 支付意图与外部交易状态机 | 区分创建、处理中、成功、失败、关闭、未知；未知状态不得再次扣款 |
| BIZ-RSV-15 | REQ-RSV-15 | P0 | 回调验签与幂等 | 验签、时间窗、防重放、外部交易号唯一、重复/乱序回调无副作用 |
| BIZ-RSV-16 | REQ-RSV-16 | P0 | 支付结果查询与超时恢复 | 通过渠道主动查询/回调重放恢复；客户端超时不推定支付失败 |
| BIZ-RSV-17 | REQ-RSV-17 | P0 | 订单/支付/库存 Hold 协同 | 支付成功、过期释放、取消、并发回调竞态有事务/锁/补偿规则 |
| BIZ-RSV-18 | REQ-RSV-18 | P1 | 渠道结算与 PMS 账务对账 | 外部交易、Payment、LedgerEntry、退款逐笔对账；差异进入人工队列，不自动改账 |
| BIZ-FIN-01 | REQ-FIN-01 | P0 | 账本追加不可变 | 账务只追加，错误通过冲销/调整凭证修复 |
| BIZ-FIN-02 | REQ-FIN-02 | P0 | 退款状态机补齐 | 申请、待审批、批准待执行、执行中、成功、失败、拒绝 |
| BIZ-FIN-03 | REQ-FIN-03 | P0 | 审批与执行职责分离 | 审批人不得执行自己批准的高风险操作，遵循 SoD |
| BIZ-FIN-04 | REQ-FIN-04 | P0 | 并发幂等账务写入 | 唯一凭证键、事务和重试，不出现重复收款/退款 |
| BIZ-FIN-05 | REQ-FIN-05 | P1 | 营业日归属 | Payment、CashierShift、LedgerEntry 的 business_date 口径明确 |
| BIZ-FIN-06 | REQ-FIN-06 | P1 | Folio Transfer 配对 | 转出/转入双边凭证、property、currency、transfer_group 一致 |
| BIZ-FIN-07 | REQ-FIN-07 | P1 | 账务对账 | ChargeLine、TaxLine、Payment、Refund、LedgerEntry、Folio 核对 |
| BIZ-FIN-08 | REQ-FIN-08 | P1 | 发票记录 | 若纳入业务范围，定义发票状态、外部编号、幂等和冲红关系 |
| BIZ-FIN-09 | REQ-FIN-09 | P1 | 收银班次 | 开班、交班、结班、差异、复核和营业日关联 |
| BIZ-FIN-10 | REQ-FIN-10 | P2 | 财务报表 | 以 LedgerEntry 和正式科目口径为准 |
| BIZ-FIN-11 | REQ-FIN-11 | P2 | 财务导出 | 字段最小化、权限、理由、审计和导出水印（如批准） |
| BIZ-AUD-01 | REQ-AUD-01 | P0 | 营业日唯一打开规则 | 同物业同时最多一个 OPEN |
| BIZ-AUD-02 | REQ-AUD-02 | P0 | 夜审全量阻断项 | 预览收集所有 blockers，不遇首个错误即退出 |
| BIZ-AUD-03 | REQ-AUD-03 | P0 | 执行互斥与幂等 | 同物业同营业日不可并发重复执行 |
| BIZ-AUD-04 | REQ-AUD-04 | P1 | 夜审运行历史 | 每次运行状态、耗时、触发者、失败码、结果摘要 |
| BIZ-AUD-05 | REQ-AUD-05 | P1 | 营业日列表 API | 当前、历史、待关闭、异常状态 |
| BIZ-AUD-06 | REQ-AUD-06 | P1 | 失败恢复/重试 | 可重试错误与不可重试错误分类，支持人工恢复 |
| BIZ-AUD-07 | REQ-AUD-07 | P1 | 营业日重开 | 权限、原因、审批/复核、审计和后续重跑 |
| BIZ-AUD-08 | REQ-AUD-08 | P1 | 自动调度命令 | DB 锁、超时、重试、运行记录和告警 |
| BIZ-AUD-09 | REQ-AUD-09 | P2 | 夜审差异报告 | 账务、入住、库存和营业日核对 |
| BIZ-HK-01 | REQ-HK-01 | P0 | 房间作业状态与可售状态分离 | 清洁状态不直接覆盖房间主状态 |
| BIZ-HK-02 | REQ-HK-02 | P0 | 任务生成幂等 | 退房/维修事件重复触发不重复建任务 |
| BIZ-HK-03 | REQ-HK-03 | P1 | 清洁任务分配/执行/检查 | PENDING→IN_PROGRESS→DONE→INSPECTED/REJECTED |
| BIZ-HK-04 | REQ-HK-04 | P1 | 维修状态机 | OPEN→IN_PROGRESS→RESTORED/CANCELLED |
| BIZ-HK-05 | REQ-HK-05 | P1 | 净房放行 | 指定角色/复核规则，审计原因 |
| BIZ-HK-06 | REQ-HK-06 | P1 | 超时任务队列 | SLA、超时升级、转交和通知 |
| BIZ-HK-07 | REQ-HK-07 | P1 | 房态库存联动 | 任务变化由服务统一计算可售性 |
| BIZ-HK-08 | REQ-HK-08 | P2 | 维修成本/停房分析 | 有可靠来源后再纳入报表 |
| BIZ-CMP-01 | REQ-CMP-01 | P0 | AuditEvent 数据库级不可变 | 应用层和 DB 层双重保护 |
| BIZ-CMP-02 | REQ-CMP-02 | P0 | 审计 PII 最小化 | 不记录完整 query string、证件明文、token、密码 |
| BIZ-CMP-03 | REQ-CMP-03 | P1 | 事件覆盖率矩阵 | 关键写操作 100% 有审计 |
| BIZ-CMP-04 | REQ-CMP-04 | P1 | 审计检索/详情权限 | 独立权限、物业范围、导出控制 |
| BIZ-CMP-05 | REQ-CMP-05 | P1 | 留存与归档策略 | 按数据类别保留、归档、删除/匿名化 |
| BIZ-CMP-06 | REQ-CMP-06 | P1 | 完整性校验 | hash/checkpoint/归档校验和和恢复演练 |
| BIZ-CMP-07 | REQ-CMP-07 | P2 | 合规操作报告 | 高风险揭示、导出、Break-glass、审批复核 |
| BIZ-RPT-01 | REQ-RPT-01 | P0 | 指标口径注册表 | 每个指标定义公式、数据源、时区、过滤条件和版本 |
| BIZ-RPT-02 | REQ-RPT-02 | P0 | 财务指标权威数据源 | 收入以 LedgerEntry/批准口径，不直接累计 Payment |
| BIZ-RPT-03 | REQ-RPT-03 | P1 | 经营日报/月报 | 入住率、ADR、RevPAR 等指标定义与分母一致 |
| BIZ-RPT-04 | REQ-RPT-04 | P1 | 预订渠道/取消/No-show 分析 | 使用稳定来源码和事件时间 |
| BIZ-RPT-05 | REQ-RPT-05 | P1 | 房态清洁维修分析 | 任务耗时、停房、清洁时长和房间可售率 |
| BIZ-RPT-06 | REQ-RPT-06 | P1 | 报表权限与物业范围 | 角色、数据范围、敏感字段和导出策略 |
| BIZ-RPT-07 | REQ-RPT-07 | P2 | 异步大报表 | 任务状态、下载权限、过期清理 |
| BIZ-RPT-08 | REQ-RPT-08 | P2 | 指标版本和历史重算 | 公式变化不静默改变历史口径 |
| BIZ-WN-01 | REQ-WN-01 | P0 | 待办源事件去重 | dedupe_key 唯一，事件重复不重复生成 |
| BIZ-WN-02 | REQ-WN-02 | P0 | 源业务状态校验 | 待办完成前重新校验源对象状态和权限 |
| BIZ-WN-03 | REQ-WN-03 | P1 | 分配/转交/处理/关闭 | 状态机、处理人、原因和审计 |
| BIZ-WN-04 | REQ-WN-04 | P1 | 通知投递可靠性 | outbox/重试/失败队列/幂等 |
| BIZ-WN-05 | REQ-WN-05 | P1 | 离职员工待办转交 | 主管接管或队列回收，不丢任务 |
| BIZ-WN-06 | REQ-WN-06 | P1 | 通知偏好与静默 | 不影响安全/财务强制通知 |
| BIZ-WN-07 | REQ-WN-07 | P2 | 站内通知中心 | 未读、已读、跳转源业务对象 |
| BIZ-WN-08 | REQ-WN-08 | P2 | 运营待办 SLA | 超时、升级、统计 |

### 15.2 项目治理、数据库与身份权限任务

| ID | 优先级 | 任务 | 交付物/验收 |
|---|---|---|---|
| PM-01 | P0 | PMS3 范围与治理章程冻结 | 版本范围、禁止项、决策流程和阶段门禁经负责人确认 |
| PM-02 | P0 | PMS3 代码基线冻结 | 最新 main SHA/依赖/环境报告 |
| PM-03 | P0 | 全项目需求追踪矩阵 | REQ→BIZ→代码/字段/API/UI/权限/测试/证据 |
| PM-04 | P0 | 领域边界和唯一写入口矩阵 | 权威模型/服务调用图 |
| PM-05 | P1 | 模块依赖规则检查 | CI 静态检查，新增反向依赖失败 |
| PM-06 | P1 | 架构决策记录 ADR | 重要技术决策可追溯 |
| PM-07 | P1 | 技术债和弃用 API 台账 | 责任人/期限/影响 |
| PM-08 | P2 | 依赖升级策略 | 锁文件和升级报告 |
| DB-01 | P0 | Schema/字段/约束/索引全盘点 | 数据字典 |
| DB-02 | P0 | Guest/员工/会员重复数据审查 | 重复与合并报告 |
| DB-03 | P0 | 员工工号唯一和再入职数据模型 | 约束+迁移+测试；业务规则引用 DEC-01/02 |
| DB-04 | P0 | GuestPhone 共享号码规则 | 人工复核流程 |
| DB-05 | P0 | GuestDocument 密钥版本/轮换 | 双版本解密和恢复测试 |
| DB-06 | P0 | Reservation 政策快照 | 创建时快照和历史测试 |
| DB-07 | P0 | 退款状态/审批执行分离 | 状态机和 SoD 测试 |
| DB-08 | P0 | LedgerEntry 唯一凭证/冲销约束 | 并发唯一性测试 |
| DB-09 | P0 | Payment/CashierShift 营业日字段 | 业务日对账 |
| DB-10 | P0 | FolioTransfer 配对事实 | 双边凭证完整性 |
| DB-11 | P0 | BusinessDate/NightAudit 运行约束 | 单 OPEN/单 RUNNING |
| DB-12 | P0 | AuditEvent DB 级不可变 | 普通账号 UPDATE/DELETE 失败 |
| DB-13 | P1 | InventoryHold 生命周期和索引 | 过期/释放/清理 |
| DB-14 | P1 | IdempotencyRecord NULL/并发规则 | MySQL 8 专项测试 |
| DB-15 | P1 | RateLimitCounter 清理/分区评估 | 数据规模报告 |
| DB-16 | P1 | LegacyRecordLink 字段/约束完整性核对 | 核对来源唯一键、目标引用、状态、校验和及复核字段；模型已存在则复用 |
| DB-17 | P1 | 旧字段清理和 Contract 迁移 | 引用扫描/备份/恢复 |
| DB-18 | P1 | 查询排序字段治理 | 稳定排序和索引 |
| DB-19 | P2 | 报表预聚合/读模型评估 | 性能证据，不先行造读模型 |
| DB-20 | P2 | 数据保留/匿名化自动任务 | 留存审计 |
| IAM-01 | P0 | User/Profile/Membership/Assignment 边界核对 | 关系图 |
| IAM-02 | P0 | 员工离职会话和授权撤销 | E2E/安全测试 |
| IAM-03 | P0 | 员工再入职历史策略 | 决策+模型测试 |
| IAM-04 | P0 | 九角色权限矩阵复核 | 角色×动作×数据范围 |
| IAM-05 | P0 | User.groups 残留授权读取清理 | 全仓检索和测试 |
| IAM-06 | P1 | 员工邀请生命周期 | token 过期/撤销/重放 |
| IAM-07 | P1 | 授权原因和有效期 | Assignment 历史 |
| IAM-08 | P1 | SoD 规则 | 审批/执行隔离 |
| IAM-09 | P1 | 会话强制撤销 | 离职/密码重置后验证 |
| IAM-10 | P2 | 权限变更审计报表 | 高风险授权列表 |

### 15.3 服务、迁移、API、UI、基础设施与质量任务

| ID | 优先级 | 任务 | 交付物/验收 |
|---|---|---|---|
| SVC-01 | P0 | EmployeeLifecycle 唯一写服务 | 服务调用图/绕过写入口扫描 |
| SVC-02 | P0 | Guest/GuestDocument/GuestMerge 写服务收口 | AR-03 清偿；合并事务与补偿测试 |
| SVC-03 | P0 | InventoryAvailability/InventoryHold 服务 | 并发不超卖、hold 幂等/过期 |
| SVC-04 | P0 | Reservation/Stay 生命周期服务 | 状态机/多房/No-show/退房一致性 |
| SVC-05 | P0 | Billing/Folio/Ledger/Refund/Approval 服务 | AR-04/05 收口；账务唯一写入口 |
| SVC-06 | P1 | BusinessDate/NightAudit 服务 | 运行锁/恢复/营业日一致性 |
| SVC-07 | P1 | Housekeeping/Maintenance/RoomRelease 服务 | 房态/清洁/维修/库存统一状态迁移 |
| SVC-08 | P1 | WorkItem/Notification 服务边界 | AR-02/08 收口；Outbox 事件消费者 |
| SVC-09 | P1 | Reporting Metric Registry 服务 | 指标版本/黄金样本 |
| SVC-10 | P0 | 统一 Idempotency/Outbox/Retry | 同事务 Outbox、租约锁、退避、死信、重放审计 |
| MIG-01 | P0 | 源数据盘点 | 表/字段/记录/外部依赖清单 |
| MIG-02 | P0 | 备份与恢复准备 | 迁移前恢复演练 |
| MIG-03 | P0 | 迁移预览与冲突分类 | dry-run/冲突报告/白名单 |
| MIG-04 | P0 | 幂等分批回填 | 断点续跑/重复执行无副作用 |
| MIG-05 | P0 | 数据记录/金额/状态/关系对账 | 差异为零或有批准解释 |
| MIG-06 | P0 | 冻结全部旧写路径 | 全路由 diff；含 /no_show_one/，DEC-24 |
| MIG-07 | P1 | 只读观察期 | 新旧读取差异监控 |
| MIG-08 | P1 | 旧审计中间件迁移与归档 | 先迁移 hotelapp.audit_middleware 再退役旧包 |
| MIG-09 | P1 | 独立 Contract migration | 备份、引用扫描、回滚/前向修复 |
| MIG-10 | P1 | 全仓旧入口引用扫描 | 路由/模板/JS/命令/任务/外部集成零活写 |
| API-01 | P0 | 统一错误码和响应封套 | 契约测试 |
| API-02 | P0 | Request ID 全链路 | 日志关联 |
| API-03 | P0 | API 权限矩阵 | 逐路由覆盖 |
| API-04 | P0 | 对象级/物业级授权 | 水平越权测试 |
| API-05 | P1 | 分页/排序/日期规范 | 契约测试 |
| API-06 | P1 | OpenAPI CI 漂移检查 | CI job |
| API-07 | P1 | Apifox 环境和集合 | 实际导入/执行报告 |
| API-08 | P0 | 旧写入口收口 | /no_show_one/ 等全部旧写路由负向测试 |
| API-09 | P1 | 导出/敏感字段安全 | 逐字段权限、理由、限流、审计 |
| API-10 | P0 | API 安全负向测试 | CSRF/越权/重放/限流/Token query 泄露 100% |
| UI-01 | P1 | 统一设计 Token/组件 | UI 指南 |
| UI-02 | P1 | 11 中心页面状态/动作一致性 | 页面检查 |
| UI-03 | P1 | 所有详情路由可达 | 404/403 E2E |
| UI-04 | P1 | 桌面/窄屏回归 | 截图报告 |
| INF-01 | P0 | Redis 多 worker 部署验证 | 共享/故障测试（G09 阶段四复核） |
| INF-02 | P0 | 后台任务锁/重试/记录 | 中断恢复/多进程互斥报告 |
| INF-03 | P0 | MySQL 备份恢复演练 | 恢复报告 |
| INF-04 | P1 | 健康检查/readiness | 部署测试 |
| INF-05 | P1 | 结构化日志和监控告警 | 告警演练 |
| INF-06 | P1 | 密钥轮换和配置隔离 | 安全检查 |
| INF-07 | P1 | Python/Django 版本升级评估 | 兼容矩阵；独立提交 |
| INF-08 | P1 | RPO/RTO 和异地备份 | 灾备演练 |
| GATE-01 | P0 | PMS2 最终验收报告核验 | 必须为“通过” |
| GATE-02 | P0 | 最新 main SHA 冻结 | SHA/工作区/提交链 |
| GATE-03 | P0 | 11 中心代码现状盘点 | 代码/模型/服务/API/UI矩阵 |
| GATE-04 | P0 | 需求追踪矩阵 | REQ→BIZ→实现→测试→证据 |
| GATE-05 | P0 | 未决业务决策冻结 | 全局/局部阻塞清单 |
| GATE-06 | P0 | 备份恢复前置验证 | 可恢复证据 |
| GATE-07 | P0 | CI/MySQL/Redis/浏览器环境确认 | 可复现环境清单 |
| QA-01 | P0 | 单元/服务/API/集成/E2E | 强制用例通过 |
| QA-02 | P0 | 九角色权限矩阵 | 菜单/API/对象/字段负向用例 |
| QA-03 | P0 | MySQL 8 空库与升级迁移 | 双路径报告 |
| QA-04 | P0 | 并发/幂等/死锁/重试 | 竞态测试 |
| QA-05 | P0 | Redis 多 worker 和故障降级 | 断连/恢复/限流策略 |
| QA-06 | P0 | 夜审/后台任务重复与中断恢复 | 无重复过账/可重放 |
| QA-07 | P1 | 性能压测和慢查询 | 冻结容量目标报告 |
| QA-08 | P0 | 备份恢复与数据不变量 | 业务抽样/账务对账 |
| QA-09 | P0 | 安全扫描和依赖审查 | 无未关闭 P0/P1 安全项 |
| QA-10 | P1 | UI 桌面/窄屏回归 | 真实浏览器证据 |
| REL-01 | P0 | 发布/回滚手册 | 演练记录 |
| REL-02 | P0 | 最终验收证据包 | 证据索引与复现步骤 |
| REL-03 | P0 | 独立验收签署 | 最终通过报告 |
| REL-04 | P0 | 生产配置/密钥核验 | check --deploy 与人工复核 |
| REL-05 | P1 | 迁移顺序和维护窗口 | 发布 runbook |
| REL-06 | P1 | 监控告警与值班流程 | 告警演练 |
| REL-07 | P1 | 业务培训和操作手册 | 培训/签收 |
| REL-08 | P0 | 发布后观察与回滚决策 | 观察期指标与决策记录 |
| REL-09 | P0 | 生产支付供应商验收 | 渠道沙箱/生产回调/对账；不得用 mock 代替 |


## 16. 技术栈与版本升级策略

### 16.1 当前技术栈约束

当前仓库历史环境包含 Django 4.1/Python 3.9。此组合存在版本生命周期和安全维护风险。PMS3 启动前必须做专项兼容评估，不能把框架升级与大量业务模型迁移混在一个提交。

### 16.2 推荐升级路线

建议目标评估：
- Python 3.12；
- Django 5.2 LTS；
- MySQL 8；
- Redis 稳定支持版本；
- 生产 WSGI Server；
- 依赖锁定文件和漏洞扫描。

此为推荐目标，不代表仓库当前已经升级。执行前必须：
1. 列出 Django/Python/第三方依赖兼容矩阵；
2. 运行完整测试；
3. 修复弃用 API；
4. 评估 middleware、URL、ORM、模板、认证、session 行为；
5. 独立提交框架升级；
6. MySQL 8 全量回归；
7. 保留可回滚部署包。

不建议在同一阶段同时升级 Django、重构全部 App、替换前端和迁移数据库。

### 16.3 依赖治理

- 使用 requirements 锁定或等效锁文件；
- 区分 runtime/dev/test 依赖；
- CI 安装可复现；
- 禁止不维护的依赖；
- 依赖升级需测试和安全扫描；
- 生产使用正式 WSGI server，不用开发服务器。

---

## 17. 文档与工程治理

### 17.1 必须维护的文档

- 本方案：`docs/PMS3.0全项目开发最终方案.md`（V2）；
- 架构审查报告：`docs/audit/phase0/80-architecture-review.md`；
- 公共预订生命周期审查报告：`docs/audit/phase0/90-public-booking-order-lifecycle-review.md`；
- PMS3 开发任务台账；
- 数据字典；
- API 契约；
- 状态机和业务流程；
- 权限矩阵；
- 迁移/回滚手册；
- UI 规范；
- 测试计划和测试报告；
- 部署运维手册；
- 变更日志；
- 发布说明和最终验收报告。

### 17.2 每次提交必须包含

1. 任务 ID；
2. 修改范围；
3. 关联模型/字段/migration；
4. API 变化；
5. 页面/菜单变化；
6. 权限/审计影响；
7. 测试命令和结果；
8. 文档更新；
9. 风险/回滚说明；
10. commit SHA。

### 17.3 禁止行为

- 不允许静默推送；
- 不允许将未实现写成已实现；
- 不允许无任务编号的无边界改造；
- 不允许修改已发布 migration；
- 不允许直接在生产数据库手工改表而不留 migration；
- 不允许删除账务/审计/审批历史；
- 不允许使用 User.groups 作为第二权限权威；
- 不允许页面直接修改核心业务模型；
- 不允许通过缓存替代数据库约束；
- 不允许用 Mock 数据验收正式功能；
- 不允许未经审批增加敏感字段权限；
- 不允许将 PMS2 未完成范围转移到 PMS3 掩盖验收失败。

---

## 18. Definition of Done（单任务完成定义）

每一个 PMS3 任务必须同时满足：

1. 有明确任务 ID 和已批准需求；
2. 有业务规则和异常流程；
3. 字段已进入数据字典，或明确复用现有字段；
4. migration 已创建并通过 MySQL 8；
5. 领域服务是唯一写入口；
6. API 契约和权限码已登记；
7. 页面/菜单已接线（若需求要求）；
8. 状态转换、幂等、并发和事务边界正确；
9. 审计、通知、数据范围和敏感字段符合决策；
10. 单元、服务、API、MySQL、集成和 E2E 测试通过；
11. OpenAPI、Apifox 和文档同步；
12. 代码已提交 main，提交说明包含任务 ID；
13. 独立复核代码和测试；
14. 验收证据可复现。

任一项缺失，状态不得标记为“已验收”。

---

## 19. PMS 3.0 最终验收标准

最终验收必须同时满足：

- 范围内全部任务状态为“已验收”或经批准明确排除；
- P0/P1/P2 未关闭缺陷为 0；
- 11 个业务中心全部通过；
- 核心跨中心闭环全部通过；
- MySQL 8 空库迁移和升级通过；
- Redis 多 worker 和故障降级通过；
- 九角色菜单/API/对象范围/字段权限通过；
- 所有关键写路径通过服务层；
- 所有财务凭证可对账，账本不可变；
- 夜审、后台任务、通知具有幂等和恢复能力；
- 审计关键事件覆盖率 100%，数据库级保护通过；
- OpenAPI 与实际 API 无漂移；
- E2E 强制用例 100% 通过；
- 性能、备份恢复、监控告警达到冻结目标；
- 旧写入口为 0，数据映射/归档完整；
- 文档、代码、迁移、测试和发布版本一致；
- 独立验收签署通过。

最终结论只能为：
- 通过；
- 有条件整改；
- 不通过。

只有“通过”允许 PMS 3.0 正式发布。不得以 CI 绿灯或开发者自测替代独立验收。公共预订 11 场景及 SEC-01/02、真实支付渠道生产验收、支付/账本/库存一致性故障用例必须纳入最终证据包。

---

## 20. 负责人待确认决策清单

以下问题在对应开发前必须由负责人明确，不允许开发人员自行假设：

| 编号 | 决策事项 | 建议原则 | 阻塞范围 |
|---|---|---|---|
| DEC-01 | 员工再入职：恢复原 Membership 还是新建任职记录 | 保留历史；倾向新建任职事实并关联历史员工 | 员工中心/数据库 |
| DEC-02 | 工号格式和是否允许人工修改 | 物业范围唯一；正式工号不可随意修改 | 员工中心 |
| DEC-03 | Guest 合并权限与复核人 | 双人复核/高权限操作；保留来源映射 | Guest 360 |
| DEC-04 | Guest 性别/国籍/生日/语言字段必要性 | 数据最小化，明确业务用途后再加 | Guest 360 |
| DEC-05 | 证件留存期限及法律保留例外 | 依据经营地法律和业务规则制定 | Guest 360 |
| DEC-06 | Guest 合并后是否允许撤销 | 需可追溯补偿；不能承诺无条件自动拆分 | Guest 360 |
| DEC-07 | Walk-in 无预订入住是否纳入 | 明确纳入/排除；纳入需独立账务来源 | 预订入住/财务 |
| DEC-08 | 取消政策快照字段结构 | JSON 快照+版本引用，保留计算结果 | 预订 |
| DEC-09 | DepositPolicy 版本和押金计算规则 | 版本化并保存预订时快照 | 预订/财务 |
| DEC-10 | Refund 审批/执行职责分离细节 | 申请、审批、执行分离；高风险双人控制 | 财务 |
| DEC-11 | 收银班次差异容忍与审批 | 不自动抹平差异；差异必须说明和复核 | 财务 |
| DEC-12 | 营业日截止时间与物业时区 | 每物业明确配置和变更生效时间 | 夜审/报表 |
| DEC-13 | 夜审重开审批和复核 | 原则上高权限+理由+审计+重跑对账 | 夜审 |
| DEC-14 | 净房放行是否需要双人复核 | 依房型/维修等级/风险定义 | 清洁维修 |
| DEC-15 | 会员等级规则和权益 | 不在未定义权益前引入积分/储值 | 会员 |
| DEC-16 | 报表指标口径 | 财务账本优先，物业时区和营业日明确 | 报表 |
| DEC-17 | 通知渠道 | 首期站内通知；短信/邮件需另行批准和供应商接入 | 通知 |
| DEC-18 | Redis 故障时限流策略 | 敏感入口 fail-closed 或 DB 安全降级 | 基础设施 |
| DEC-19 | Python/Django 升级目标 | 评估 Python 3.12 + Django 5.2 LTS | 技术栈 |
| DEC-20 | 多物业能力 | 当前单物业运营优先，不启用未经验收的切换能力 | 平台/身份 |
| DEC-21 | 性能并发和容量目标 | 依据真实硬件和业务峰值冻结 | 性能 |
| DEC-22 | 数据留存和匿名化周期 | 依经营地法规、财务和争议留存要求确定 | 数据治理 |
| DEC-23 | 审计读取是否落审计（防递归的安全事件策略） | 查询/导出审计列表默认不落审计；高危揭示与导出走专门安全事件通道 | 审计合规（CMP-04；承继 A16 登记项，2026-11-07 架构审查新增） |
| DEC-24 | 旧写入口 `/no_show_one/` 退役确认 | 确认旧工作台（/checkin/、/booking/ 页面）无依赖后并入 410 护栏；No-show 唯一权威命令为 PMS2 API | 迁移退役（MIG-06；承继审查报告 AR-01，2026-11-07 新增） |
| DEC-25 | 生产支付渠道/商户/回调协议 | 明确支付供应商、币种、验签、密钥轮换、退款能力、查询接口、结算文件及沙箱 | REQ-RSV-13~18、FIN、REL-09 |
| DEC-26 | 库存可售公式与互斥扣减口径 | 冻结总量、已售、Hold、停售、停房的互斥定义及逐日计算规则 | REQ-INV-01~03、DB-13、QA-04 |
| DEC-27 | Outbox 事件目录与失败策略 | 冻结事件名称/版本/消费者、重试、死信、人工重放权限和告警 | REQ-WN-04、SVC-08/10、INF-02 |
| DEC-28 | RPO/RTO 与备份保留目标 | 明确最大数据丢失窗口、恢复时限、备份保留和演练频率 | INF-03/08、QA-08、REL-01 |
| DEC-29 | 生产部署拓扑与运行边界 | 冻结 Nginx/WSGI/worker/DB/Redis/任务进程/文件存储/监控的部署方式与资源上限 | INF-01~08、REL-04~06 |

未决事项分为“全局阻塞”和“局部阻塞”。局部决策未确认时，只允许开发不依赖该决策的其他任务，不允许自行填入默认业务规则。

---

## 21. 开发人员接手说明

开发人员收到本文件后按以下顺序执行：

1. 阅读本方案及 PMS2 正式决策、验收标准；
2. 确认 PMS2 最终验收报告为“通过”；若不是，停止 PMS3 正式业务开发；
3. 拉取最新 main，记录 SHA；
4. 执行 Phase 0 基线盘点；
5. 先提交任务追踪矩阵和字段/API/页面/服务现状差距；
6. 识别现有能力，复用现有模型和服务；
7. 将任务拆成可独立审查的小提交；
8. 按 Phase 1 至 Phase 7 的依赖实施；
9. 每个任务完成后更新文档、测试、变更日志和状态；
10. 阶段退出条件满足后再进入下一阶段；
11. 最终提交完整证据包和独立验收报告。

### 21.1 每个任务的工单模板

- 任务 ID：
- 任务名称：
- 优先级：
- 业务目标：
- 当前代码现状：
- 目标行为：
- 数据模型/字段：
- migration：
- 唯一写服务：
- API/权限码：
- 页面/菜单：
- 状态机：
- 幂等/并发/事务：
- 审计/通知：
- 数据迁移/兼容：
- 测试用例：
- 验收证据：
- 依赖任务：
- 风险/回滚：
- 负责人：
- 当前状态：
- commit SHA：

---

## 22. 版本修订与生效

本文件经负责人审阅确认后冻结为 PMS 3.0 最终开发基线。修订必须记录：
- 变更章节；
- 原因；
- 影响的任务 ID；
- 数据库/API/权限/页面/测试影响；
- 批准人；
- 生效日期。

### 修订记录

| 日期 | 修订 | 原因与依据 | 影响范围 | 批准人 |
|---|---|---|---|---|
| 2026-11-07 | V1 架构现状校准 | 80-architecture-review.md（基线 635d9dd）及 A16 后续证据 | 形成 §3.4、§14.0、DEC-23/24；历史修订保留 | 待负责人确认 |
| 2026-11-07 | V2 针对性修订 | 最新 main 7a2c9b8；90-public-booking-order-lifecycle-review.md；全量任务编号交叉核对 | §0/§2 门禁状态校准；§5 需求编号改为 REQ；§14 阶段仅引用任务台账；§15 重建唯一执行任务注册表；新增 REQ-RSV-13~18、DEC-25~29、§23~26；明确 mock 与真实支付边界；不新增业务中心 | 待负责人确认 |
不得在开发中通过口头沟通改变字段语义、权限边界、审批规则、账务行为或历史数据处理规则。

**最终原则：先确认 PMS2 验收通过，再启动 PMS3；先盘点再设计，先设计再开发；一个业务事实只保留一个权威来源，一个业务动作只保留一个权威写入口，所有开发必须有测试和可复核证据。**


## 23. 跨中心数据一致性与业务不变量（V2 强制规范）

本章定义系统级不变量。它们不是新业务模块，而是各业务任务必须共同遵守的正确性约束。每条不变量至少要有：数据库约束（适用时）、服务层校验、并发测试、异常恢复测试和审计/对账证据。

| 编号 | 不变量 | 权威事实与验证要求 |
|---|---|---|
| INV-A01 | Guest 主档唯一权威 | 预订联系人、实际入住人、付款人是不同关系角色；共享手机号不能自动合并；合并保留来源和补偿链 |
| INV-A02 | 员工身份/任职/授权分离 | User、StaffProfile、PropertyStaffMembership、RoleAssignment 各自表达不同事实；离职撤销授权和会话 |
| INV-A03 | 库存不超卖 | 同一物业/房型/住宿日的有效可售量不得被并发占用超过可售库存；MySQL 是最终约束权威 |
| INV-A04 | Hold 与 Reservation 一致 | Hold 只能由唯一服务创建/确认/释放；过期任务不得释放已确认/已支付订单的有效库存 |
| INV-A05 | Reservation 与 Stay 分离 | 预订状态不替代实际入住状态；多房订单按 ReservationRoom/Stay 粒度履约 |
| INV-A06 | Payment 与账本分离 | 外部交易、Payment、LedgerEntry 分属不同事实；一个外部成功交易最多产生一笔有效 Payment/入账事实 |
| INV-A07 | 退款不超过可退余额 | 同一支付的累计成功退款不得超过已成功收款减去已退款金额；审批与执行职责分离 |
| INV-A08 | 账本追加不可变 | 已入账 LedgerEntry 不 UPDATE/DELETE；更正通过有来源关联的冲销/调整凭证 |
| INV-A09 | Folio 余额可重算 | 余额由有效 LedgerEntry 按币种计算；不同币种不得混加；缓存不是权威 |
| INV-A10 | 营业日唯一且可追溯 | 每物业同一时刻最多一个 OPEN 营业日；occurred_at、created_at、posted_at、business_date 不可混用 |
| INV-A11 | 夜审单次过账 | 同物业同营业日同一逻辑夜审最多一组有效过账；失败重试不得重复生成凭证 |
| INV-A12 | 房间可售状态可解释 | 占用、清洁、维修是独立维度；净房检查未通过或维修停房时不得进入可售库存 |
| INV-A13 | 待办/通知不成为源业务 | WorkItem/Notification 是派生任务/投递副本；已读不代表源业务完成 |
| INV-A14 | 报表口径可复现 | 指标公式、来源、时区、营业日、币种、过滤状态、版本和快照固定后结果可重算 |
| INV-A15 | 物业隔离贯穿全链路 | Middleware/API/Service/ORM 查询均使用服务端 property context；客户端 property_id 不构成授权 |
| INV-A16 | 审计覆盖关键写操作 | 创建、状态迁移、敏感揭示、审批、执行、导出、异常恢复均有 actor/property/request_id/result |

禁止用 Redis 锁代替 MySQL 事务/唯一约束；禁止通过前端按钮禁用代替后端幂等；禁止以定时任务“最终会修复”作为允许产生错误事实的理由。

### 23.1 跨中心黄金链路

以下链路每条必须有成功、失败、重复、并发和中断恢复用例：

1. Public Booking → GuestService → InventoryHold → Reservation → PaymentIntent → Provider Transaction → Payment → LedgerEntry。
2. Reservation → ReservationRoom → Stay/StayGuest/StayRoomSegment → Folio/ChargeLine → Checkout → HousekeepingTask → RoomInspection → InventoryAvailability。
3. Refund Request → ApprovalRecord → Provider Refund → Refund → LedgerEntry → Reconciliation。
4. BusinessDate → NightAudit Preview/Blocker → NightAuditRun → LedgerPosting → Report Snapshot。
5. Employee Termination → Membership/RoleAssignment Revocation → Session Revocation → WorkItem Reassignment → AuditEvent。

## 24. 公共预订、支付和订单生命周期专项规范

### 24.1 当前实现边界（main@7a2c9b8）

已由代码/专项报告证明的能力：
- BookingAccessToken 使用 secrets.token_urlsafe(32) 生成 256-bit 随机令牌；
- 数据库保存 SHA-256 摘要，不保存明文；
- 订单详情只接受 X-Booking-Token 请求头，URL query token 一律拒绝；
- 查询错误凭证不返回订单详情，查询限流并写审计；
- 创建时一次性展示令牌，复制有反馈；找回后脱敏；
- 公共自助支付/取消按订单状态门控；重复支付意图返回同一意图；
- 现有支付回调为受控 mock 路径，自动化测试覆盖 11 个生命周期场景；
- 订单 Token 不创建顾客登录账户。

证据：docs/audit/phase0/90-public-booking-order-lifecycle-review.md；apps/api/test_public_booking_lifecycle.py；提交 7a2c9b8。

### 24.2 未完成能力与禁止误报

当前代码证据不能证明已接入真实支付服务商。生产支付适配、商户密钥、真实回调验签协议、渠道查询、退款通道、结算文件对账、支付争议处理和生产告警均不得标记为已实现。开发环境 mock 支付通过不等于生产支付验收通过。

### 24.3 状态与账务分层

至少区分以下事实，不允许合并成一个状态字段：
- Reservation：PENDING/CONFIRMED/CANCELLED/EXPIRED/CHECKED_IN/CHECKED_OUT/NO_SHOW；
- PaymentIntent：PMS 发起的支付意图及其生命周期；
- ProviderTransaction：支付服务商交易状态和交易编号；
- Payment：PMS 已确认的收款业务事实；
- LedgerEntry：账本已入账凭证；
- InventoryHold：库存临时占用状态。

必须明确每个状态的唯一写服务、触发来源、允许迁移、超时规则、幂等键、并发锁、补偿动作和审计事件。

### 24.4 支付/库存异常处理

必须覆盖：
- 客户端支付请求超时但渠道已扣款；
- 渠道回调重复、乱序、延迟或签名错误；
- 渠道成功但 PMS 数据库短暂不可用；
- Payment 已创建但 LedgerEntry 未成功；
- 订单过期任务与支付成功回调同时执行；
- 用户取消与支付回调同时执行；
- 退款渠道成功但 PMS 未收到响应；
- 渠道退款失败、未知或部分退款；
- 订单多房/部分履约下的退款分摊。

处理原则：先向渠道查询真实交易状态，再按幂等业务命令恢复；未知状态不重复扣款；不得直接改历史账本；差异进入可追踪人工队列。

## 25. 生产部署、故障恢复与灾备规范

### 25.1 最小生产拓扑

生产部署至少明确：Nginx/HTTPS/静态资源、Django WSGI Web Worker、独立后台任务执行进程、MySQL 8 主业务数据库、Redis（缓存/限流/协调，不存权威业务事实）、文件/导出/证件密文存储、结构化日志/指标/告警/审计归档。

不要求 Kubernetes 或微服务。单机部署也必须进程隔离、资源上限、服务守护和启动顺序清晰。

### 25.2 故障策略

| 依赖故障 | 必须定义的行为 |
|---|---|
| MySQL 不可用 | 核心写操作失败关闭；不得本地缓存后假装成功 |
| Redis 不可用 | 按风险 fail-closed 或受控 DB 降级；敏感接口不得无保护放开 |
| 支付渠道不可用 | 支付意图进入可查询未知/处理中状态；禁止盲目重复扣款 |
| 后台任务中断 | 租约过期可接管；任务幂等；有运行记录和告警 |
| Outbox 投递失败 | 源事务已提交，事件保留待重试；死信可审查和重放 |
| 文件存储不可用 | 不返回虚假下载成功；导出任务可恢复/过期 |
| 密钥不可用 | 敏感字段 fail-closed；不得降级明文 |

### 25.3 RPO/RTO 与恢复验收

DEC-28 冻结目标后，执行全量/增量/binlog 备份和恢复演练。恢复后必须验证：员工登录、角色权限、Guest 查询、预订/入住状态、支付/退款/账本对账、营业日/夜审运行事实、附件与密钥可用。仅恢复数据库文件不算恢复成功。

## 26. 全项目任务追踪、验收证据与变更控制

### 26.1 唯一编号规则

- REQ-*：§5 业务需求编号，只用于需求引用，不作为 commit/task ID。
- BIZ-*：§15.1 业务需求执行任务，必须一一映射一个或多个 REQ。
- PM/DB/IAM/SVC/MIG/API/UI/INF/GATE/QA/REL-*：跨中心工程任务，编号只在 §15 注册一次。
- 任何阶段不得临时创造同前缀、同编号但不同语义的任务。
- Phase 章节只引用 §15 已注册任务，不重新定义任务；任何未登记任务先登记再开发。
- 一个 BIZ 业务包必须按阶段拆为可独立合并的子任务：BIZ-xxx-S01（DB/数据）、S02（Service）、S03（API/权限）、S04（UI）、S05（QA/验收）；只创建实际适用的子任务。子任务 ID 不得与全局任务注册表冲突，提交必须同时引用父 BIZ ID 和来源 REQ ID。工程任务的子任务采用父任务 ID-S01 格式。

### 26.2 追踪关系

每条需求必须形成：REQ → BIZ → 数据模型/字段 → Domain Service → API/权限 → UI → 测试用例 → 验收证据 → Commit SHA。

纯基础设施/治理任务可不关联 UI，但必须说明不适用原因。

### 26.3 任务状态与验收证据

每个任务记录：任务 ID、需求 ID、优先级、Phase、依赖、当前代码现状、目标行为、模型字段、唯一写服务、API、权限、页面、审计、幂等/并发、异常补偿、迁移/回滚、测试、验收证据、负责人、状态、Commit SHA。

任务状态只允许沿 §1.3 状态机流转。无测试证据不得进入“已开发待验收”；无独立复核不得进入“已验收”。

### 26.4 阶段映射与任务依赖

- Phase 0：GATE/PM 基线任务；
- Phase 1：DB 任务 + BIZ 子任务 S01；
- Phase 2：SVC 任务 + BIZ 子任务 S02；
- Phase 3：API 任务 + BIZ 子任务 S03；
- Phase 4：UI 任务 + BIZ 子任务 S04；
- Phase 5：MIG 任务；
- Phase 6：QA 任务 + BIZ 子任务 S05；
- Phase 7：REL 任务。

一个 BIZ 父任务只有在适用阶段子任务全部通过后才能标记“已验收”。阶段任务不得复制业务需求，不得另造与 BIZ 父任务同义的任务。

### 26.5 变更控制

改变字段语义、状态机、权限、审批、金额、营业日、库存公式、支付供应商或历史迁移规则时，必须先新增/更新 DEC 或 ADR，记录影响 REQ/BIZ/DB/API/UI/QA 任务。未经负责人批准，不允许以代码提交事实替代业务决策。
