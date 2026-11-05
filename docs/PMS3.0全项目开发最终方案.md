# PMS 3.0 全项目开发最终方案

> 文档性质：PMS 3.0 最终开发方案与任务执行基线（待负责人确认后冻结）
> 目标仓库：`wulei0214/django-hotel2`
> 基线分支：`main`
> 编制日期：2026-11-06
> 前置项目：PMS 2.0
> 重要门禁：本文件允许现在进行方案评审、任务拆解、代码现状盘点和准备工作；**PMS 3.0 正式业务代码开发必须等 PMS 2.0 最终验收报告结论为“通过”后启动**。当前仓库 A16 开发者申报版的结论仍为“有条件整改”，G09 Redis、G10 性能容量、阶段四本地业务验收及 G14 独立签署尚未完成。本方案不得被解释为 PMS 2.0 已通过，也不得绕过该门禁。
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

本方案编制时读取的仓库 main 基线为：

`8c30d007544aa8b24d5c57b64cac3a97cee44093`

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

当前 A16 报告记录的开发侧测试为 SQLite 729 项、MySQL 729 项通过，但报告本身明确属于开发者申报，不构成最终验收。报告记录 Redis 尚未部署、性能压测未执行、空库迁移待本地 DBA 授权、独立验收待签署。因此不能据此声称 PMS2 已完成最终验收。

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

#### 功能任务

| ID | 优先级 | 功能任务 | 规则/完成条件 |
|---|---|---|---|
| EMP-01 | P0 | 员工身份/档案/任职/授权模型核对 | 明确 User、StaffProfile、Membership、Assignment 关系 |
| EMP-02 | P0 | 员工编号规则 | 物业范围唯一；正式工号不可随意修改；离职工号保留且不可复用 |
| EMP-03 | P0 | 再入职模型 | 决策“恢复原任职”还是“新建任职记录”；不得被简单唯一约束阻断 |
| EMP-04 | P1 | 入职/邀请/激活 | 管理员创建邀请；一次性 token、过期、撤销、使用审计 |
| EMP-05 | P1 | 调岗/换部门/角色变更 | 记录生效时间、原因、操作者、原值和新值 |
| EMP-06 | P1 | 暂停/离职/恢复 | 立即撤销有效会话和岗位授权；保留历史 |
| EMP-07 | P1 | 员工详情时间线 | 任职、角色、账号、密码重置和状态历史 |
| EMP-08 | P1 | 员工批量导入/导出 | 仅批准字段；预览、校验、冲突报告、权限和审计 |
| EMP-09 | P2 | 员工数据质量盘点 | 未绑定 User、无 Membership、重复工号和失效授权报表 |
| EMP-10 | P2 | 账号安全操作 | 管理员重置密码、强制改密、禁用、会话撤销 |

#### 关键字段设计（新增前必须查重）

建议核对/补齐：
- `PropertyStaffMembership.employee_no`：物业范围工号；
- `PropertyStaffMembership.employment_status`：当前物业任职状态；
- `start_at`、`end_at`：任职时间；
- `rehire_of_id` 或等效关系：再入职来源（只有业务确认后新增）；
- `StaffRoleAssignment.effective_from/effective_to`：授权有效期；
- `StaffRoleAssignment.reason_code/reason_text`：授权原因（现状需核查，不得假设已有）；
- `Invitation.expires_at/used_at/revoked_at`：邀请生命周期。

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

#### 功能任务

| ID | 优先级 | 功能任务 | 规则/完成条件 |
|---|---|---|---|
| GST-01 | P0 | Guest 唯一主档与重复识别策略 | 手机号不能单独证明自然人唯一 |
| GST-02 | P0 | 联系方式可信度/共享手机号 | 共享号码人工复核、primary_guest_key 规则明确 |
| GST-03 | P0 | 证件密钥版本与轮换 | 密钥版本可识别，轮换可恢复，禁止无密钥解密 |
| GST-04 | P1 | Guest 合并工作流 | 提案、审核、合并、撤销/补偿、关联迁移和审计 |
| GST-05 | P1 | 实际入住人关系 | ReservationGuest、StayGuest、StayRoomSegment 真实关联 |
| GST-06 | P1 | Guest 时间线 | 预订、入住、房间段、账单、会员、投诉/服务事件 |
| GST-07 | P1 | 客人偏好与联系授权 | 仅纳入有明确用途的字段，记录来源和同意版本 |
| GST-08 | P1 | 证件留存和到期清理 | retention_until、法律保留例外、删除任务审计 |
| GST-09 | P2 | Guest 数据质量 | 重复、缺字段、共享手机号、失效证件统计 |
| GST-10 | P2 | 客人档案导出 | 最小化、审批/理由、字段脱敏、审计 |

#### 字段设计

需评估而非默认新增：
- Guest：`gender`、`nationality`、`preferred_language`、`date_of_birth`（涉及敏感和必要性审查）；
- GuestPhone：`verified_at`、`shared_review_required`、`primary_guest_key`、`source`；
- GuestDocument：`key_version`、`retention_until`、`doc_type`、`doc_no_hash`；
- GuestMergeRequest：source_guest、target_guest、reason、status、reviewer、approved_at、executed_at、reversal reference（仅在模型不存在时设计）。

必须为证件类型+证件哈希建立符合物业/证件政策的索引，避免明文证件号索引。

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

#### 功能任务

| ID | 优先级 | 功能任务 | 规则/完成条件 |
|---|---|---|---|
| INV-01 | P0 | 可售库存唯一计算规则 | 逐日房型库存、已售、冻结、停售、维修和占用一致 |
| INV-02 | P0 | 并发库存锁与版本控制 | 乐观版本/行锁/唯一约束组合，不能超卖 |
| INV-03 | P0 | InventoryHold 生命周期 | 创建、确认、释放、过期、清理、重试可追溯 |
| INV-04 | P1 | 房态工作台 | 房间、房型、楼层、清洁、维修和入住状态视图 |
| INV-05 | P1 | 维修/清洁影响库存 | 状态变化通过服务同步，不直接改库存计数 |
| INV-06 | P1 | 价格日历和 RatePlan | 有效期、币种、税费、关闭日、版本历史 |
| INV-07 | P1 | 房间自然排序 | 楼栋/楼层/房号排序规则，不能纯字符串排序 |
| INV-08 | P1 | 批量房态操作 | 预览影响、权限、理由、逐项结果和审计 |
| INV-09 | P2 | 库存差异监控 | 每日核对 reservation/hold/room inventory |
| INV-10 | P2 | 价格变更审计 | 记录旧值、新值、原因、生效时间和操作者 |

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

#### 功能任务

| ID | 优先级 | 功能任务 | 规则/完成条件 |
|---|---|---|---|
| MEM-01 | P0 | Guest 与会员关系唯一性 | 一位 Guest 在同一会员体系下的关系规则明确 |
| MEM-02 | P1 | 会员等级变更服务 | 权限、原因、生效时间和历史不可覆盖 |
| MEM-03 | P1 | 暂停/恢复/注销 | 状态机和重新激活规则 |
| MEM-04 | P1 | 会员历史与权益摘要 | 只展示已批准范围内真实权益 |
| MEM-05 | P2 | 会员标签/偏好 | 仅基于合法来源和业务用途 |
| MEM-06 | P2 | 会员报表 | 会员数量、等级变动和活跃口径冻结 |

字段评估：Membership status、level、effective_from/to、source、reason、changed_by；历史使用独立 history，不覆盖旧等级。不得擅自引入积分、储值、折扣钱包等财务实体。

服务/API/页面：GuestMembershipService；会员列表、详情、等级历史、暂停/恢复、关联修复；会员详情从 Guest 进入，不创建另一套 Guest profile。

验收：重复关联被阻止；Guest 合并时会员关系处理正确；等级历史不可篡改；无会员权益时不显示虚构权益。

### 5.5 预订与入住中心（Reservation & Stay）

**职责边界：** Reservation 表示预订合同；Stay 表示实际履约入住；ReservationGuest 和 StayGuest 表示人员关系；StayRoomSegment 表示实际房间和时间段。

#### 功能任务

| ID | 优先级 | 功能任务 | 规则/完成条件 |
|---|---|---|---|
| RSV-01 | P0 | Reservation/Stay 状态机统一 | 状态定义、合法迁移、终态规则明确 |
| RSV-02 | P0 | 预订并发与幂等 | 公共/后台/前台入口统一服务，防重复和超卖 |
| RSV-03 | P0 | 取消政策快照 | 创建时保存适用政策版本、计算结果和时点 |
| RSV-04 | P0 | 押金政策版本与事实快照 | 预订确认时保存适用政策和金额规则 |
| RSV-05 | P1 | 多房预订履约 | 部分入住、部分退房、换房和取消独立处理 |
| RSV-06 | P1 | Walk-in 业务决策 | 明确是否支持无预订直接入住；若支持，建立合法来源和账务链；若不支持，明确拒绝入口 |
| RSV-07 | P1 | No-show 与过期释放统一 | 合并重复命令逻辑，单一调度入口和锁 |
| RSV-08 | P1 | 延住/换房/提前离店 | 重新核验库存、价格、押金、账单和房间段 |
| RSV-09 | P1 | 预订变更历史 | 保存旧值、新值、原因、操作者和政策影响 |
| RSV-10 | P1 | 公开预约防刷 | 访问令牌、速率限制、库存占用和订单查询保护 |
| RSV-11 | P2 | 预订来源统计 | 来源代码、渠道、活动等口径冻结 |
| RSV-12 | P2 | 订单/入住异常队列 | 超期、状态冲突、缺少入住人、房间段断链 |

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

#### 服务/API/页面

- 服务：ReservationService、ReservationPolicySnapshotService、StayLifecycleService、RoomAssignmentService、ReservationExpiryService。
- API：可订查询、创建、详情、修改、取消、确认、排房、入住、续住、换房、退房、No-show、过期释放、订单查询。
- 页面：预订列表/详情、预订时间线、房间分配、入住登记、住中操作、退房结账、异常队列。
- 公共预约和员工工作台使用同一领域服务，但认证/授权/DTO/限流策略不同。

#### 验收

- 同一请求重试不重复创建预订；
- 并发不超卖；
- 取消政策按创建时快照执行；
- 多房订单部分入住/退房准确；
- 实际入住人和预订联系人不混淆；
- 延住、换房会重新核算库存和价格；
- 过期释放命令只有一个权威实现；
- 订单查询不泄露其他客人信息。

### 5.6 财务与账单中心（Finance & Billing）

**职责边界：** 账单和总账事实由 Billing 服务管理；页面不直接写 Payment、Refund、LedgerEntry 或 Folio。

#### 功能任务

| ID | 优先级 | 功能任务 | 规则/完成条件 |
|---|---|---|---|
| FIN-01 | P0 | 账本追加不可变 | 账务只追加，错误通过冲销/调整凭证修复 |
| FIN-02 | P0 | 退款状态机补齐 | 申请、待审批、批准待执行、执行中、成功、失败、拒绝 |
| FIN-03 | P0 | 审批与执行职责分离 | 审批人不得执行自己批准的高风险操作，遵循 SoD |
| FIN-04 | P0 | 并发幂等账务写入 | 唯一凭证键、事务和重试，不出现重复收款/退款 |
| FIN-05 | P1 | 营业日归属 | Payment、CashierShift、LedgerEntry 的 business_date 口径明确 |
| FIN-06 | P1 | Folio Transfer 配对 | 转出/转入双边凭证、property、currency、transfer_group 一致 |
| FIN-07 | P1 | 账务对账 | ChargeLine、TaxLine、Payment、Refund、LedgerEntry、Folio 核对 |
| FIN-08 | P1 | 发票记录 | 若纳入业务范围，定义发票状态、外部编号、幂等和冲红关系 |
| FIN-09 | P1 | 收银班次 | 开班、交班、结班、差异、复核和营业日关联 |
| FIN-10 | P2 | 财务报表 | 以 LedgerEntry 和正式科目口径为准 |
| FIN-11 | P2 | 财务导出 | 字段最小化、权限、理由、审计和导出水印（如批准） |

#### 字段/模型评估

- Refund：状态、approved_by、approved_at、execution_started_at、executed_at、failure_code、failure_message（脱敏）；
- ApprovalRecord：申请人、审批人、执行人、SoD、状态、幂等、拒绝/过期/重申请；
- Payment：business_date、received_by（按事实归属确定）；
- CashierShift：business_date、opened_by、closed_by、差异说明；
- FolioTransfer：property、currency、transfer_group、source_entry、target_entry；
- LedgerEntry：唯一业务凭证键、void_of、entry_type、business_date；
- InvoiceRecord/ReconciliationRun：与业务源记录建立唯一幂等关联。

数据库约束必须处理“先查后建”并发竞态；不能仅靠 Python 检查唯一性。

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

#### 功能任务

| ID | 优先级 | 功能任务 | 规则/完成条件 |
|---|---|---|---|
| AUD-01 | P0 | 营业日唯一打开规则 | 同物业同时最多一个 OPEN |
| AUD-02 | P0 | 夜审全量阻断项 | 预览收集所有 blockers，不遇首个错误即退出 |
| AUD-03 | P0 | 执行互斥与幂等 | 同物业同营业日不可并发重复执行 |
| AUD-04 | P1 | 夜审运行历史 | 每次运行状态、耗时、触发者、失败码、结果摘要 |
| AUD-05 | P1 | 营业日列表 API | 当前、历史、待关闭、异常状态 |
| AUD-06 | P1 | 失败恢复/重试 | 可重试错误与不可重试错误分类，支持人工恢复 |
| AUD-07 | P1 | 营业日重开 | 权限、原因、审批/复核、审计和后续重跑 |
| AUD-08 | P1 | 自动调度命令 | DB 锁、超时、重试、运行记录和告警 |
| AUD-09 | P2 | 夜审差异报告 | 账务、入住、库存和营业日核对 |

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

#### 功能任务

| ID | 优先级 | 功能任务 | 规则/完成条件 |
|---|---|---|---|
| HK-01 | P0 | 房间作业状态与可售状态分离 | 清洁状态不直接覆盖房间主状态 |
| HK-02 | P0 | 任务生成幂等 | 退房/维修事件重复触发不重复建任务 |
| HK-03 | P1 | 清洁任务分配/执行/检查 | PENDING→IN_PROGRESS→DONE→INSPECTED/REJECTED |
| HK-04 | P1 | 维修状态机 | OPEN→IN_PROGRESS→RESTORED/CANCELLED |
| HK-05 | P1 | 净房放行 | 指定角色/复核规则，审计原因 |
| HK-06 | P1 | 超时任务队列 | SLA、超时升级、转交和通知 |
| HK-07 | P1 | 房态库存联动 | 任务变化由服务统一计算可售性 |
| HK-08 | P2 | 维修成本/停房分析 | 有可靠来源后再纳入报表 |

#### 字段评估

- HousekeepingTask：assigned_to、priority、due_at、completed_at、inspected_by、rejected_reason、source_event_key；
- MaintenanceTicket：severity、reported_by、assigned_to、started_at、restored_at、out_of_service_from/to；
- RoomInspection：inspection_result、inspector、reason、checked_at。
- 若当前模型已具备字段则复用；不因方案重复加字段。

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

#### 功能任务

| ID | 优先级 | 功能任务 | 规则/完成条件 |
|---|---|---|---|
| CMP-01 | P0 | AuditEvent 数据库级不可变 | 应用层和 DB 层双重保护 |
| CMP-02 | P0 | 审计 PII 最小化 | 不记录完整 query string、证件明文、token、密码 |
| CMP-03 | P1 | 事件覆盖率矩阵 | 关键写操作 100% 有审计 |
| CMP-04 | P1 | 审计检索/详情权限 | 独立权限、物业范围、导出控制 |
| CMP-05 | P1 | 留存与归档策略 | 按数据类别保留、归档、删除/匿名化 |
| CMP-06 | P1 | 完整性校验 | hash/checkpoint/归档校验和和恢复演练 |
| CMP-07 | P2 | 合规操作报告 | 高风险揭示、导出、Break-glass、审批复核 |

#### 技术要求

- AuditEvent 不允许业务账号 UPDATE/DELETE；
- MySQL 账号权限、触发器或受控写入通道形成数据库级防护；
- 审计事件包括 actor、property、action、object、reason、request_id、timestamp、result；
- 审计查询本身避免无限递归记录，采用专门安全事件策略；
- 日志和审计分离：日志排障，审计证明业务行为；
- 任何导出不得默认输出完整敏感字段。

### 5.10 报表分析中心（Reporting & Analytics）

#### 功能任务

| ID | 优先级 | 功能任务 | 规则/完成条件 |
|---|---|---|---|
| RPT-01 | P0 | 指标口径注册表 | 每个指标定义公式、数据源、时区、过滤条件和版本 |
| RPT-02 | P0 | 财务指标权威数据源 | 收入以 LedgerEntry/批准口径，不直接累计 Payment |
| RPT-03 | P1 | 经营日报/月报 | 入住率、ADR、RevPAR 等指标定义与分母一致 |
| RPT-04 | P1 | 预订渠道/取消/No-show 分析 | 使用稳定来源码和事件时间 |
| RPT-05 | P1 | 房态清洁维修分析 | 任务耗时、停房、清洁时长和房间可售率 |
| RPT-06 | P1 | 报表权限与物业范围 | 角色、数据范围、敏感字段和导出策略 |
| RPT-07 | P2 | 异步大报表 | 任务状态、下载权限、过期清理 |
| RPT-08 | P2 | 指标版本和历史重算 | 公式变化不静默改变历史口径 |

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

#### 功能任务

| ID | 优先级 | 功能任务 | 规则/完成条件 |
|---|---|---|---|
| WN-01 | P0 | 待办源事件去重 | dedupe_key 唯一，事件重复不重复生成 |
| WN-02 | P0 | 源业务状态校验 | 待办完成前重新校验源对象状态和权限 |
| WN-03 | P1 | 分配/转交/处理/关闭 | 状态机、处理人、原因和审计 |
| WN-04 | P1 | 通知投递可靠性 | outbox/重试/失败队列/幂等 |
| WN-05 | P1 | 离职员工待办转交 | 主管接管或队列回收，不丢任务 |
| WN-06 | P1 | 通知偏好与静默 | 不影响安全/财务强制通知 |
| WN-07 | P2 | 站内通知中心 | 未读、已读、跳转源业务对象 |
| WN-08 | P2 | 运营待办 SLA | 超时、升级、统计 |

通知是投递副本，WorkItem 是待处理任务，源业务对象仍是业务事实权威。禁止通过“标记通知已读”直接改变预订/退款/维修源状态。

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

### Phase 0：启动门禁与基线复核（P0）

- GATE-01 核实 PMS2 最终验收报告是否为“通过”；
- GATE-02 固定 PMS3 启动 SHA；
- GATE-03 复核 11 个中心实际代码状态；
- GATE-04 建立需求/代码/字段/API/页面/测试追踪矩阵；
- GATE-05 冻结未决业务决策；
- GATE-06 备份与恢复验证；
- GATE-07 建立 CI/MySQL/Redis/浏览器环境。

退出标准：无未决 P0 决策；基线可复现；所有任务有责任人和验收定义。

### Phase 1：领域模型与数据库治理（P0）

- DB-01 全量字段字典；
- DB-02 重复字段/冗余模型识别；
- DB-03 外键/唯一/检查约束治理；
- DB-04 索引和查询计划；
- DB-05 员工再入职和工号模型；
- DB-06 Guest 合并、共享手机号、证件密钥版本；
- DB-07 Reservation 政策快照/取消事实；
- DB-08 Payment/Refund/CashierShift/FolioTransfer/Approval 数据事实；
- DB-09 NightAudit/BusinessDate 并发和运行历史；
- DB-10 AuditEvent DB 级不可变和留存；
- DB-11 InventoryHold 和幂等记录清理；
- DB-12 LegacyRecordLink 和旧数据映射；
- DB-13 MySQL 8 迁移与升级矩阵。

退出标准：字段定义完整、迁移可执行、数据可对账、无关键约束缺口。

### Phase 2：核心领域服务和状态机（P0/P1）

- SVC-01 EmployeeLifecycle；
- SVC-02 Guest/GuestDocument/GuestMerge；
- SVC-03 InventoryAvailability/InventoryHold；
- SVC-04 Reservation/Stay 生命周期；
- SVC-05 Billing/Ledger/Refund/Approval；
- SVC-06 BusinessDate/NightAudit；
- SVC-07 Housekeeping/Maintenance/RoomRelease；
- SVC-08 WorkItem/Notification；
- SVC-09 Reporting Metric Registry；
- SVC-10 统一 Idempotency/Outbox/Retry 机制。

退出标准：所有写路径收口到服务；状态机、事务、幂等、并发和审计测试通过。

### Phase 3：API 契约与安全治理（P0/P1）

- API-01 统一响应与错误码；
- API-02 统一 Request ID；
- API-03 API 路由和权限矩阵；
- API-04 对象/物业范围授权；
- API-05 分页/排序/日期窗口；
- API-06 幂等和限流；
- API-07 OpenAPI/Apifox 漂移检查；
- API-08 旧写入口收口；
- API-09 导出/敏感字段安全；
- API-10 API 安全负向测试。

退出标准：所有写 API 可追踪到服务；没有未授权访问或绕过服务的写入口。

### Phase 4：前端工作台与跨中心闭环（P1）

建议按业务关键路径交付：

1. 预订与入住中心；
2. 房态与库存中心；
3. Guest 360；
4. 清洁与维修；
5. 财务与账单；
6. 夜审与营业日；
7. 员工中心；
8. 会员中心；
9. 通知与待办；
10. 报表分析；
11. 审计与合规。

每个中心按列表→详情→操作→异常队列→权限测试顺序交付。不能先完成静态页面再长期等待 API；前后端使用冻结的 API 契约并行。

退出标准：所有页面真实接 API，无 Mock 假数据，核心跨模块流程可端到端执行。

### Phase 5：迁移、旧入口退役与数据对账（P0/P1）

- MIG-01 源数据盘点；
- MIG-02 备份/恢复；
- MIG-03 迁移预览与冲突分类；
- MIG-04 幂等回填；
- MIG-05 记录数/金额/状态/关联对账；
- MIG-06 冻结旧写路径；
- MIG-07 只读观察；
- MIG-08 归档；
- MIG-09 独立退役 migration；
- MIG-10 全仓引用扫描和旧路由验证。

退出标准：无双写、无双算、无未解释孤儿、旧入口不能绕过新服务。

### Phase 6：自动化测试、性能和韧性（P0/P1/P2）

- QA-01 单元/服务/API/集成/E2E；
- QA-02 九角色权限矩阵；
- QA-03 MySQL 8 双路径迁移；
- QA-04 并发/幂等/死锁/重试；
- QA-05 Redis 多 worker 和故障降级；
- QA-06 夜审/后台任务重复执行；
- QA-07 性能压测和慢查询；
- QA-08 备份恢复；
- QA-09 安全扫描和依赖审查；
- QA-10 UI 桌面/窄屏回归。

退出标准：P0/P1/P2 为零；强制测试全过；性能和恢复指标达到冻结值。

### Phase 7：上线准备与独立验收（P0）

- REL-01 发布说明；
- REL-02 配置/密钥检查；
- REL-03 迁移顺序和维护窗口；
- REL-04 回滚/前向修复方案；
- REL-05 监控告警；
- REL-06 业务培训和操作手册；
- REL-07 独立验收；
- REL-08 负责人签署；
- REL-09 发布后观察和回滚决策。

退出标准：证据包完整、独立验收通过、上线审批完成。

---

## 15. 全项目任务清单（可直接拆分工单）

以下清单是开发任务主目录。每个任务拆成独立 issue/工单时，必须补齐负责人、预估、依赖、代码路径、测试和证据。未复核的字段/接口标为设计候选，不得直接迁移。

### 15.1 项目治理与架构

| ID | 优先级 | 任务 | 交付物/验收 |
|---|---|---|---|
| PM-01 | P0 | PMS2 最终验收通过门禁 | 正式签署报告 |
| PM-02 | P0 | PMS3 代码基线冻结 | SHA/依赖/环境报告 |
| PM-03 | P0 | 全项目需求追踪矩阵 | 每项需求映射代码/测试 |
| PM-04 | P0 | 领域边界和唯一写入口矩阵 | 服务调用图 |
| PM-05 | P1 | 模块依赖规则检查 | CI 静态检查 |
| PM-06 | P1 | 架构决策记录 ADR | 重要技术决策可追溯 |
| PM-07 | P1 | 技术债和弃用 API 台账 | 责任人/期限/影响 |
| PM-08 | P2 | 依赖升级策略 | 锁文件和升级报告 |

### 15.2 数据库和数据治理

| ID | 优先级 | 任务 | 交付物/验收 |
|---|---|---|---|
| DB-01 | P0 | Schema/字段/约束/索引全盘点 | 数据字典 |
| DB-02 | P0 | Guest/员工/会员重复数据审查 | 重复与合并报告 |
| DB-03 | P0 | 员工工号唯一和再入职模型 | 约束+迁移+测试 |
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
| DB-16 | P1 | LegacyRecordLink 真实迁移 | 旧记录映射对账 |
| DB-17 | P1 | 旧字段清理和 Contract 迁移 | 引用扫描/备份/恢复 |
| DB-18 | P1 | 查询排序字段治理 | 稳定排序和索引 |
| DB-19 | P2 | 报表预聚合/读模型评估 | 性能证据 |
| DB-20 | P2 | 数据保留/匿名化自动任务 | 留存审计 |

### 15.3 身份、员工与权限

| ID | 优先级 | 任务 | 交付物/验收 |
|---|---|---|---|
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

### 15.4 Guest 与会员

| ID | 优先级 | 任务 | 交付物/验收 |
|---|---|---|---|
| GST-01 | P0 | Guest 与实际入住人模型核对 | 关系矩阵 |
| GST-02 | P0 | 共享手机号复核 | 权限/审计/E2E |
| GST-03 | P0 | 证件加密密钥轮换 | 恢复演练 |
| GST-04 | P1 | Guest 合并提案/审批/执行 | 可追溯合并 |
| GST-05 | P1 | Guest 360 时间线 | 真实关联数据 |
| GST-06 | P1 | 证件留存和到期清理 | dry-run/审计 |
| GST-07 | P1 | 会员状态/等级历史 | 状态机 |
| GST-08 | P2 | Guest 数据质量报表 | 重复/缺失/共享 |
| GST-09 | P2 | 客人偏好字段必要性评估 | 数据最小化决议 |

### 15.5 房态库存与预订入住

| ID | 优先级 | 任务 | 交付物/验收 |
|---|---|---|---|
| INV-01 | P0 | 库存可售公式冻结 | 规则文档+测试 |
| INV-02 | P0 | 库存并发锁和版本 | MySQL 并发测试 |
| INV-03 | P0 | InventoryHold 幂等/过期 | 重复任务测试 |
| INV-04 | P1 | 房间自然排序 | 排序规则+索引 |
| INV-05 | P1 | RatePlan/DailyRate 版本历史 | 价格变更追溯 |
| INV-06 | P1 | 清洁维修影响可售性 | E2E |
| RSV-01 | P0 | Reservation/Stay 状态机 | 状态迁移矩阵 |
| RSV-02 | P0 | 预订创建统一幂等 | API 重放测试 |
| RSV-03 | P0 | 取消政策快照 | 政策版本回归 |
| RSV-04 | P0 | DepositPolicy 版本/快照 | 金额规则测试 |
| RSV-05 | P1 | 多房部分入住/退房 | E2E |
| RSV-06 | P1 | Walk-in 是否纳入决策 | 正式决策记录 |
| RSV-07 | P1 | 过期预订/No-show 单一命令 | 重复/并发执行 |
| RSV-08 | P1 | 续住/换房/提前离店 | 库存和账务一致 |
| RSV-09 | P1 | 预订历史与取消原因 | 审计时间线 |
| RSV-10 | P2 | 异常订单队列 | 管理工作台 |

### 15.6 财务、夜审、清洁维修

| ID | 优先级 | 任务 | 交付物/验收 |
|---|---|---|---|
| FIN-01 | P0 | Refund 状态机扩展 | 申请/批准/执行/失败 |
| FIN-02 | P0 | 审批执行分离 | SoD 负向测试 |
| FIN-03 | P0 | LedgerEntry 并发唯一性 | MySQL 并发测试 |
| FIN-04 | P0 | LedgerEntry 不可变 | DB 权限测试 |
| FIN-05 | P1 | Payment/Shift business_date | 营业日对账 |
| FIN-06 | P1 | FolioTransfer 双边凭证 | 配对完整性 |
| FIN-07 | P1 | Charge/Tax/Ledger 对账 | 黄金样本 |
| FIN-08 | P1 | 夜审 blocker 全量收集 | 多错误场景测试 |
| FIN-09 | P1 | 夜审运行历史和恢复 | 失败重试 |
| FIN-10 | P1 | 夜审 DB 锁和调度 | 多进程互斥 |
| HK-01 | P0 | 清洁任务事件幂等 | 重复事件测试 |
| HK-02 | P0 | 房间放行与库存状态机 | 权限/并发测试 |
| HK-03 | P1 | 清洁分配/检查/驳回 | 状态机 E2E |
| HK-04 | P1 | 维修工单和停房联动 | E2E |
| HK-05 | P1 | 超时任务和升级 | 调度测试 |

### 15.7 审计、报表、通知

| ID | 优先级 | 任务 | 交付物/验收 |
|---|---|---|---|
| CMP-01 | P0 | AuditEvent 数据库级保护 | DB 负向测试 |
| CMP-02 | P0 | 日志/审计 PII 清理 | 日志扫描 |
| CMP-03 | P1 | 关键业务审计覆盖率 | 事件矩阵 |
| CMP-04 | P1 | 审计留存归档 | 校验和/恢复 |
| RPT-01 | P0 | 指标定义注册表 | 指标字典 |
| RPT-02 | P0 | 财务指标账本口径 | 与 LedgerEntry 对账 |
| RPT-03 | P1 | 经营日报/月报 | 黄金样本 |
| RPT-04 | P1 | 报表权限/导出 | 越权和审计 |
| RPT-05 | P2 | 大报表异步导出 | 任务/下载/过期 |
| WN-01 | P0 | 待办 dedupe_key | 并发重复消费测试 |
| WN-02 | P0 | 源对象状态回查 | 失效待办测试 |
| WN-03 | P1 | 待办分配/转交/关闭 | 状态机 E2E |
| WN-04 | P1 | 通知 outbox/重试 | 故障恢复 |
| WN-05 | P1 | 离职待办转交 | 员工生命周期 E2E |
| WN-06 | P2 | 通知偏好和运营统计 | 权限/数据口径 |

### 15.8 API、前端、基础设施与交付

| ID | 优先级 | 任务 | 交付物/验收 |
|---|---|---|---|
| API-01 | P0 | 统一错误码和响应封套 | 契约测试 |
| API-02 | P0 | Request ID 全链路 | 日志关联 |
| API-03 | P0 | API 权限矩阵 | 逐路由覆盖 |
| API-04 | P0 | 对象级/物业级授权 | 水平越权测试 |
| API-05 | P1 | 分页/排序/日期规范 | 契约测试 |
| API-06 | P1 | OpenAPI CI 漂移检查 | CI job |
| API-07 | P1 | Apifox 环境和集合 | 执行报告 |
| UI-01 | P1 | 统一设计 Token/组件 | UI 指南 |
| UI-02 | P1 | 11 中心页面状态/动作一致性 | 页面检查 |
| UI-03 | P1 | 所有详情路由可达 | 404/403 E2E |
| UI-04 | P1 | 桌面/窄屏回归 | 截图报告 |
| INF-01 | P0 | Redis 多 worker 部署验证 | 共享/故障测试 |
| INF-02 | P0 | 后台任务锁/重试/记录 | 运行报告 |
| INF-03 | P0 | MySQL 备份恢复演练 | 恢复报告 |
| INF-04 | P1 | 健康检查/readiness | 部署测试 |
| INF-05 | P1 | 结构化日志和监控告警 | 告警演练 |
| INF-06 | P1 | 密钥轮换和配置隔离 | 安全检查 |
| INF-07 | P1 | Python/Django 版本升级评估 | 兼容矩阵 |
| INF-08 | P2 | RPO/RTO 和异地备份 | 灾备演练 |
| REL-01 | P0 | 发布/回滚手册 | 演练记录 |
| REL-02 | P0 | 最终验收证据包 | 证据 A01-Axx |
| REL-03 | P0 | 独立验收签署 | 最终通过报告 |

---

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

- 本方案：`docs/PMS3.0全项目开发最终方案.md`；
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

只有“通过”允许 PMS 3.0 正式发布。不得以 CI 绿灯或开发者自测替代独立验收。

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

不得在开发中通过口头沟通改变字段语义、权限边界、审批规则、账务行为或历史数据处理规则。

**最终原则：先确认 PMS2 验收通过，再启动 PMS3；先盘点再设计，先设计再开发；一个业务事实只保留一个权威来源，一个业务动作只保留一个权威写入口，所有开发必须有测试和可复核证据。**
