# PMS2 平台身份、权限中心与业务全链路治理开发实施方案

> 文档类型：正式实施方案（供产品确认、开发排期、测试验收）
> 状态：待项目负责人最终确认后作为开发排期基线；本文不代表下述功能已经全部实现
> 适用仓库：wulei0214/django-hotel2
> 规划基线：main；实施前必须重新读取最新 main 并记录实际 commit SHA
> 编制日期：2026-11-04
> 关联文档：PMS2 身份认证与员工住客账户体系改造开发方案 2.0、PMS2 接口文档与测试人员使用指南、代码与文档一致性审查报告、敏感字段权限边界设计、统一业务模型与数据库治理实施方案

---

## 1. 方案目的与范围

本方案合并原平台身份域、权限矩阵、PMS2 业务数据流审查方案及本次补充评审意见，形成统一实施基线。目标不是先改页面，而是建立可以验证的身份边界、真实权限执行链、跨模块业务数据流和可重复测试体系。

本期覆盖：

1. 平台超管与 PMS 员工身份、Session、授权、导航和审计边界。
2. /permissions/ 权限矩阵从旧 hotelapp 权限口径升级为跨 App RBAC 管理中心。
3. /system/ 拆分为 PMS 员工个人信息页和平台环境信息页。
4. ModuleRegistry、RoleRegistry、SpecialPermission 及 PLANNED 模块生命周期治理。
5. 顾客预约、前台接待、预订入住、Folio、收款、退款、夜审、员工生命周期等业务链路审查和自动化验证。
6. Swagger/OpenAPI、Apifox、Python Requests + Pytest 的接口契约和测试交付。
7. 旧入口、历史模块、敏感字段和后台任务的权限与数据一致性治理。

本方案不授权开发人员擅自改变既有正式业务决策。涉及 Guest 自动合并、资金规则、退款审批规则、敏感字段边界、角色职责等事项，必须引用对应正式决策；未决事项保持阻塞，不得由开发人员自行推断。

## 2. 已确认的总体决策

### 2.1 三项总体决策

| 决策 | 已确认方向 | 实施解释 |
|---|---|---|
| 决策一：平台身份域 | 采用平台域与 PMS 域分离 | /permissions/ 与平台环境管理使用 Admin Session；PMS 员工个人信息与业务管理使用 PMS Session；/system/ 必须拆分 |
| 决策二：权限模型 | 先限时审查，目标为跨 App RBAC | 旧矩阵仅可作为迁移过渡，不得成为最终授权中心；先产出权限点清单，再分批接入真实执行点 |
| 决策三：业务验证 | 全链路审查，按 P0/P1/P2 分层 | P0 覆盖资金、夜审、预订履约和不可逆操作；P1 覆盖身份与 PII；P2 覆盖预留/退役入口冒烟 |

### 2.2 身份模型固定原则

- Django User 可以继续作为统一认证主体，不要求为平台超管和 PMS 员工拆成两张用户表。
- 共用 User 不代表共用会话、授权或业务身份。
- 平台超管必要条件为 is_active=True、is_superuser=True、is_staff=True，三项必须同时满足。
- PMS 员工必须通过有效员工档案、当前物业有效任职和有效岗位授权；平台超管身份不得自动绕过 PMS 任职门禁。
- 住客继续免注册直订，不创建长期 Django User；公开订单访问使用独立 Booking Access Token。
- User.groups 不得作为 PMS 员工岗位授权的运行时兜底。PMS 业务授权以 StaffRoleAssignment、Group/Permission 映射和 AuthorizationService 为准。
- 平台授权必须单独定义，不得借用 PMS 员工岗位权限。

### 2.3 平台域 URL 目标

| 功能 | 目标路径 | 会话域 | 授权要求 |
|---|---|---|---|
| 平台首页 | /platform/ | Admin Session | 平台超管 |
| 权限管理中心 | /platform/permissions/ | Admin Session | 平台超管 |
| 平台环境信息 | /platform/system/ | Admin Session | 平台超管 |
| Django Admin | /admin/ | Admin Session | Django Admin 规则 + 平台超管不变量 |
| PMS 员工个人页 | /system/ 或 /me/ | PMS Session | 有效任职，仅本人 |
| 员工账号管理 | /staff_accounts/，最终并入 /manage/staff/ | PMS Session | identity.staff_manage 等业务权限 |

兼容期允许旧 /permissions/ 作为跳转入口，最终统一迁移到 /platform/permissions/。是否保留长期别名由路由迁移阶段确认。

### 2.4 平台系统页与 PMS 个人页边界

| 内容 | 归属 | 目标处理 |
|---|---|---|
| 员工姓名、账号、角色、本人登录时间、会话状态、本人改密 | PMS 个人页 | 仅本人可见，不因平台超管身份扩展为全员信息 |
| 员工账号、任职、角色分配、停职/离职、密码重置 | PMS 员工管理 | 留在员工业务域，走业务权限和审计 |
| DEBUG、运行模式、版本、时区、语言、API 基址、会话策略 | 平台环境页 | Admin Session + 平台超管授权 |
| 员工 PII、证件、手机号揭示 | PMS 业务域 | 不迁入平台环境页，遵循敏感字段权限基线 |

## 3. 当前现状与实施前置审计

以下是既有代码审查和项目文档记录的风险线索，不应直接视为实施时最新事实。Phase 0 必须在最新 main 上逐项复核。

| 领域 | 已知风险/历史发现 | Phase 0 必须确认 |
|---|---|---|
| 平台会话 | 已有提交将 /permissions/ 纳入 Admin Session 路径识别 | 是否所有平台路径、cookie、登录跳转、退出流程一致 |
| 超管条件 | 曾存在只检查 is_superuser、Django Admin 又要求 is_staff 的不一致 | 登录、页面、API、初始化命令是否统一校验三项不变量 |
| /system/ | 历史 system_info 同时包含员工个人信息与平台环境信息 | 当前视图、模板、API、导航和字段实际归属 |
| 权限矩阵 | 旧 MODULES/ACTIONS/ALL_CODENAMES 以 hotelapp CRUD 为主 | 所有 app_label.codename、调用位置、授予来源和真实执行点 |
| 授权来源 | 曾发现部分代码读取 User.groups/has_perm | 全仓是否仍存在绕过 AuthorizationService 的 PMS 授权路径 |
| 模块注册 | hotelapp MODULE_REGISTRY 与 permission_admin 模块清单曾独立维护 | 是否已统一；页面、API、导出、任务门禁覆盖率 |
| 保存安全 | 历史保存逻辑存在覆盖、并发、审计与预留权限问题 | 当前实现是否已具备 diff、版本、二次确认、AuditEvent |
| 员工管理 | /manage/staff/ 与 /staff_accounts/ 写操作迁移曾未完成 | 当前读写职责、权限、CSRF、审计及迁移完成度 |
| 业务数据流 | 公开预订与前台履约跨多个 app/service | 从 Reservation 到 Stay、Folio、Ledger、房态的真实关系和事务边界 |
| 接口契约 | 项目已有 OpenAPI 入口，接口目录已有记录 | 最新 OpenAPI 覆盖路由数、方法、字段、状态码和权限是否真实 |
| 测试账号 | 历史种子脚本曾使用 User.groups.set | 当前种子/fixture 是否符合 StaffRoleAssignment 授权模型 |
| 凭据治理 | 历史文档、迁移、seed 曾出现默认密码 | 清理明文凭据、初始化与修复流程、密码轮换和测试账号边界 |

Phase 0 的审查结果必须附最新 main SHA、检查命令、涉及文件、证据和结论。没有证据的项目标为“待核验”，不能写“已完成”。

## 4. 总体实施阶段与依赖关系

| 阶段 | 名称 | 优先级 | 主要交付 | 前置依赖 |
|---|---|---|---|---|
| Phase 0 | 现状审计与权限/数据流盘点 | P0 | 路由、权限点、身份、数据流、测试基线报告 | 无 |
| Phase 1 | 平台身份域与 /system/ 拆分 | P0 | 平台认证授权边界、平台布局、个人页与环境页拆分 | Phase 0 |
| Phase 2 | 跨 App RBAC 模型审查与注册表 | P0 | ModuleRegistry、RoleRegistry、权限映射与迁移设计 | Phase 0；与 Phase 1 可并行设计 |
| Phase 3 | 权限管理中心重构 | P0 | 真实权限矩阵、保存安全、审计、影响范围 | Phase 1、Phase 2 |
| Phase 4 | PMS2 核心业务全链路与 API 契约验证 | P0 | 预约至履约、财务、退款、夜审的可重复测试 | Phase 0；权限执行口径需 Phase 2/3 稳定后完成最终授权验收 |
| Phase 5 | 身份/PII、员工中心、预留模块与旧入口治理 | P1/P2 | 生命周期闭环、敏感操作审计、模块门禁、旧入口退役 | Phase 1、Phase 2/3；依具体子项依赖 Phase 4 |

建议 Phase 1 与 Phase 2 的审计/设计工作可以并行，但平台域基础未稳定前，不开放新的平台管理功能；RBAC 注册表和权限点清单未通过评审前，不重写矩阵 UI。Phase 4 的业务功能检查可先开展只读链路审计和测试数据准备，但最终“权限正确”验收依赖 Phase 3。

---

## 5. Phase 0：现状审计与权限/数据流盘点

### 5.1 开发范围

本阶段以审计、补充测试和形成基线为主，不进行大规模结构改造。

1. 枚举所有 URL：顾客端、PMS 员工端、平台后台、Django Admin、API、旧 hotelapp 路由、导出、后台任务。
2. 对每个入口记录 HTTP 方法、登录要求、Session cookie、权限装饰器、业务服务、数据模型、审计事件和当前状态。
3. 扫描所有权限点：app_label.codename、Permission 创建迁移、Group 授权来源、调用点、视图/服务实际执行点。
4. 盘点所有角色：实际 Group、预留角色、StaffRoleAssignment 使用情况、过期/无效分配。
5. 盘点 User、StaffProfile、PropertyStaffMembership、StaffRoleAssignment 的关联与孤儿数据；只读统计，不批量删除或自动修复。
6. 追踪顾客预约数据流：公开 API → Reservation/Guest/房型明细 → 员工预订中心 → 前台抵店/入住 → Stay/房态/Folio → Payment/Ledger → 退房。
7. 追踪退款、夜审、反结账、重开营业日、No-Show、取消、续住、换房、快速退房和敏感字段揭示。
8. 检查 /permissions/、/system/、/admin/、/login/ 的认证和授权边界。
9. 检查 Swagger/OpenAPI 路由覆盖和 Apifox 导入能力；以视图实际代码为准记录字段和响应。
10. 建立测试数据库快照、测试账号矩阵和可重置测试数据方案。

### 5.2 预期涉及文件/目录

审计对象至少包括（实际路径以最新仓库为准）：

- hotel/urls.py、hotel/settings.py、hotel/middleware.py
- hotelapp/urls.py、hotelapp/views.py、hotelapp/decorators.py
- apps/api/urls.py、apps/api/views.py 及 front desk、night audit、approval、guest center 等视图模块
- apps/identity/models.py、services.py、authorization.py、middleware.py、permission_admin.py
- apps/core/api_contract.py、审计模型/服务
- apps/reservations/、apps/guests/、apps/inventory/、apps/billing/、apps/night_audit/
- templates/ 下登录、系统信息、权限矩阵、员工中心、预订/前台页面
- tests/、各 app/tests/、管理命令、seed/fixture、migrations/
- docs/接口文档、角色权限验证指南、架构说明、开发指南

### 5.3 数据库变更

不新增业务表、不删除字段、不执行批量账号/权限迁移。只允许在独立测试库中创建可回滚的审计快照和测试数据。

### 5.4 测试要求

- 跑现有 Django test suite，并记录失败、跳过和环境依赖。
- 运行 URL reverse/resolve 检查，输出路由清单。
- 静态扫描权限检查、User.groups、has_perm、is_superuser、is_staff、csrf_exempt、模块门禁。
- 使用测试账号验证平台/PMS 双 Session 同时存在、单独退出和权限拒绝。
- 使用测试数据追踪预约至前台查询的数据库关系；不执行真实资金动作。
- 形成当前 OpenAPI 与实际 URL/视图的方法对照报告。

### 5.5 验收条件

- 每条入口都有唯一记录：路径、方法、身份域、权限点、服务、模型、风险等级、状态。
- 每个权限点都有定义位置、授予来源、调用位置和是否真实执行的证据。
- 输出角色—权限矩阵差异表，明确迁移授予与矩阵展示差异。
- 输出业务数据流图和未闭环清单。
- 输出测试基线：命令、环境、数据库、SHA、通过/失败/跳过数量。
- 不得把未检查项标记为已完成；不允许在本阶段顺手删除历史数据或 migration。

---

## 6. Phase 1：平台身份域与 /system/ 拆分

### 6.1 开发范围

1. 建立统一平台身份判断：平台超管必须同时满足 is_active、is_superuser、is_staff。
2. 建立平台认证/授权装饰器或等价统一策略，例如 platform_login_required、platform_superadmin_required。
3. 平台域未登录跳转 /admin/login/；已登录但非平台超管返回 403，不重定向到登录页。
4. 页面 GET、只读 API、修改 API 分别执行服务端授权；不得认为 URL 前缀或已登录即可放行。
5. 建立 /platform/ 独立布局、导航和首页；不得 include PMS _sidebar.html，不展示 PMS 业务菜单。
6. 将权限中心迁入 /platform/permissions/；旧 /permissions/ 兼容跳转。
7. 将平台环境信息迁入 /platform/system/；PMS /system/ 或 /me/ 仅展示当前员工本人信息。
8. PropertyContextMiddleware 对平台请求跳过 PMS 物业上下文解析，避免无任职平台账号产生无意义 property_context_denied 审计噪音。
9. 平台页面返回链接指向 /platform/，PMS 页面返回链接指向 PMS 工作台。
10. 平台 Session 与 PMS Session cookie 独立；平台退出不影响 PMS，PMS 退出不影响平台。
11. 提供受控平台超管初始化/修复/自检管理命令。修复前检查现有超管，不能在没有恢复路径时锁死平台管理入口。
12. 对双身份人员优先采用两个账号：一个平台技术账号，一个 PMS 业务账号；不自动将平台身份映射为店长或其他业务角色。

### 6.2 预期涉及文件

- hotel/settings.py：SESSION_COOKIE_NAME、CSRF_USE_SESSIONS、cookie 属性等配置审查
- hotel/urls.py：平台 URL 路由注册
- apps/identity/middleware.py：平台路由跳过物业上下文
- apps/identity/ 或 apps/core/：平台身份策略、装饰器、审计服务（以 Phase 0 确定的模块边界为准）
- hotelapp/views.py：旧 /permissions/、/system/ 兼容和迁移入口
- 新增或调整 platform views、urls、templates/base、sidebar、首页和系统页模板
- management/commands/：平台超管初始化、自检、修复命令
- tests/：平台认证、授权、会话隔离、中间件和重定向测试
- docs/：平台身份域设计与运行手册

### 6.3 数据库变更

默认不新增用户表，不将平台超管与员工账号拆表。

可能需要的数据修复必须先输出 dry-run 报告，再经负责人批准执行：
- 对现有平台超管校验三项不变量；
- 识别 is_superuser=True 但 is_staff=False、is_staff=True 但 is_superuser=False 的账号；
- 识别平台账号是否错误关联 StaffProfile/Membership/RoleAssignment；
- 不自动删除或解绑账号。

若新增平台审计事件类型，优先复用现有 AuditEvent 字段；只有现有结构无法表达时才提出 migration。

### 6.4 测试要求

- 无平台 Session → /platform/permissions/ 跳 /admin/login/。
- 仅有 PMS sessionid → 不得访问平台页。
- 仅有 admin_sessionid 且满足三项不变量 → 平台页 200。
- 已登录但非超管 → 403，不跳登录。
- is_superuser=True、is_staff=False → 拒绝平台访问并提供可审计修复路径。
- is_staff=True、is_superuser=False → 不得访问平台管理中心。
- 平台 GET、读取 API、修改 API 均分别测试未登录、非超管和超管。
- 平台 POST 无 CSRF → 403；跨站 Origin → 403。
- PMS logout 后平台 Session 仍有效；Admin logout 后 PMS Session 仍有效；双登录互不覆盖。
- 平台请求不触发物业上下文拒绝审计。
- PMS 个人页只显示本人；平台系统页不返回员工 PII。
- next 参数拒绝 // 外部地址、反斜线、编码绕过等开放重定向。

### 6.5 验收条件

- 平台所有页面、API 使用统一平台授权策略。
- 平台布局、导航和返回链接完全独立。
- /system/ 已按个人信息与平台环境信息拆分。
- 三项超管不变量在初始化、登录和授权路径中一致执行。
- 双 Session 隔离测试通过。
- 不存在平台超管自动获得 PMS 业务权限的旁路。

---

## 7. Phase 2：跨 App RBAC 模型审查与注册表

### 7.1 审查范围

必须覆盖所有真实权限点，而非只扫描当前矩阵中的 hotelapp 权限。

重点 app 至少包括：
- hotelapp：历史 CRUD、财务入口及遗留高危权限
- apps.identity：员工管理、任职状态、角色分配、密码重置等
- apps.billing：收款、退款申请、退款审批、冲账/反向分录等
- apps.night_audit：执行夜审、重开营业日、紧急访问等
- apps.guests：住客资料、PII reveal、敏感导出
- apps.reservations：预订创建/修改/取消、入住履约关联
- apps.inventory：房态、库存、房价调整
- apps.api：API 层权限入口及是否委托给服务层
- 其他实际存在并拥有 Permission 的 app

### 7.2 注册表目标

**ModuleRegistry**：每个模块记录唯一 code、中文名称、app_label、status、route_prefix、权限动作映射、risk_level、是否要求物业范围、landing_url。状态使用 ACTIVE、PLANNED、RETIRED，不再用多个 bool/reserved 字段表达同一生命周期。

**RoleRegistry**：记录角色名称、对应 Group、status、editable、builtin、职责描述和风险等级。只有实际可分配的角色进入可分配列表；占位角色必须明确标记 PLANNED，不得伪装成实际 Group。

**SpecialPermission**：登记特殊高危权限，例如证件揭示、退款审批、反向分录、夜审执行、重开营业日、break-glass。记录所属模块、风险级别、是否需要理由、二次确认或审批。

动作至少区分 view、add、change、delete、approve、special；某动作不适用时明确标记 not_applicable，不得把所有权限强行压缩为 CRUD。

### 7.3 权限映射与授权规则

- 每个矩阵单元格必须映射到真实 app_label.codename。
- 每个真实权限点必须能追溯到至少一个后端执行点；仅有 Permission 记录但无执行检查，不得宣称已生效。
- 模块 registry 与路由门禁只维护一份权威定义，禁止 hotelapp MODULE_REGISTRY 和 permission_admin.MODULES 两套列表漂移。
- PMS 员工权限运行时统一走 AuthorizationService；禁止新增 user.has_perm/User.groups 作为员工授权兜底。
- 平台超管权限单独执行，不参与 PMS 角色并集。
- 员工有多条有效 StaffRoleAssignment 时按既定规则合并权限；任职/角色过期应即时失权。
- 物业范围由有效 Membership 确定；本期单店运行，不开放客户端物业切换。
- 九个真实业务角色与两个预留角色分开显示；餐饮/停车等角色即使存在，也必须明确标注其模块当前是否 PLANNED。

### 7.4 预留模块规则

- ACTIVE：功能可用，按注册表授权。
- PLANNED：只读显示为规划模块，不允许新增授权，不允许页面/API/导出/后台任务执行。
- RETIRED：停止入口执行，保留历史权限与审计，不物理删除历史记录。
- PLANNED/RETIRED 权限不得因伪造 POST 写入有效授权。
- 模块从 PLANNED 转 ACTIVE 必须经过功能完成、权限点注册、路由门禁覆盖、测试和负责人批准。
- 停用模块时保留历史授权快照或 inactive 状态；不得擅自删除后导致重新启用时无法追溯。

### 7.5 预期涉及文件

- apps/identity/permission_admin.py：旧矩阵定义和授权映射审查
- hotelapp/views.py 及模块门禁相关文件：现有 MODULE_REGISTRY 审查
- apps/identity/authorization.py、services.py、models.py
- apps/billing/、apps/night_audit/、apps/guests/、apps/reservations/、apps/inventory/ 各权限执行点
- migrations/：Permission/Group/注册表模型变更（是否新增模型待 Phase 0 评审）
- tests/：权限点映射、授权来源、模块状态和跨 App 覆盖率测试
- docs/：跨 App 权限点清单、角色注册表和迁移映射表

### 7.6 数据库变更

先做代码注册表或只读映射清单，再决定是否需要持久化注册表模型。

可能的数据库工作：
- 若 ModuleRegistry/RoleRegistry 采用数据库管理，新增模型及唯一约束、状态字段、权限映射关系。
- 若采用代码注册表，避免为静态配置引入不必要 migration。
- 需要迁移 Group.permissions 时，必须先导出 before 快照，按映射表迁移，核对 after，提供回滚脚本。
- 不得在迁移时清空非 hotelapp 权限或未知权限。
- 不得自动把旧 CRUD 权限推断为新的 approve/special 权限。
- 迁移前输出每个角色权限数量、权限码列表、有效 StaffRoleAssignment 数量和差异报告。

### 7.7 测试要求

- 每个注册权限码都能解析到真实 Permission。
- 每个 ACTIVE 权限点都有至少一个服务端执行测试。
- 每个矩阵可见项与执行点映射一致。
- PLANNED/RETIRED 权限通过伪造 POST 不能授予。
- 多角色并集、任职失效、岗位过期、无任职、错误物业均按规则拒绝。
- 验证没有旧 User.groups 残留路径绕过 AuthorizationService。
- 对未知 codename、跨 app 错误映射、重复模块 code、重复角色映射执行负向测试。
- 迁移前后 Group.permissions 快照一致性和回滚演练通过。

### 7.8 验收条件

- 形成完整跨 App 权限点清单和权限执行位置清单。
- 所有角色和模块都有 ACTIVE/PLANNED/RETIRED 状态。
- 每个矩阵单元格有唯一权限映射和真实执行证据。
- PMS 业务授权只有一个运行时权威入口。
- 迁移可重复、可审计、可回滚。
- 未完成审查的权限点不进入可授权 UI。

---

## 8. Phase 3：权限管理中心重构

### 8.1 页面目标

权限中心采用四区结构：

1. 左侧：角色列表、角色状态、实际使用人数。
2. 中央：模块 × 动作权限矩阵；ACTIVE 模块可编辑，PLANNED/RETIRED 只读标识。
3. 右侧：角色影响范围、使用该角色的员工/任职/物业数量、高危权限、PII 权限、财务审批权限和最近变更。
4. 底部/确认区：变更摘要、理由、高危变更二次确认、版本冲突提示和保存结果。

页面文案必须准确区分平台超管与 PMS 业务权限。推荐文案：“平台超管拥有平台域管理权限；PMS 业务权限仍以员工任职和岗位授权为准。”不得宣称平台超管天然拥有所有 PMS 权限。

### 8.2 读取与修改 API

- 页面 GET 必须通过平台授权策略。
- 角色读取 API 必须独立授权，不能因为 URL 位于 permissions 下就免检。
- 权限修改 API 必须独立授权、CSRF、POST-only、角色白名单、模块状态校验和版本校验。
- 只读 API 返回当前角色权限、版本号和影响范围。
- 修改 API 接收目标权限集合、版本号、修改理由；高风险变更需要一次性确认 token。
- 请求中出现未知权限码、PLANNED/RETIRED 权限或不允许编辑的角色时，整体拒绝并记录审计，不得静默过滤。
- 响应返回变更前后差异、最新版本和 request_id。

### 8.3 保存事务与审计

推荐保存流程：

1. 读取 Group 当前权限集合，构造 before。
2. 根据注册表计算 allowed，仅包含 ACTIVE 且当前操作者允许管理的权限。
3. 对 requested - allowed 执行拒绝，不静默忽略。
4. 读取并校验 version/etag，阻止并发覆盖。
5. 计算 before/after diff。
6. 高风险变更校验一次性 confirm_token，token 绑定角色、diff hash、操作者和有效期。
7. 数据库事务内写权限变更。
8. 同一事务内写 AuditEvent：action、entity_type、entity_id、actor、before_json、after_json、reason、request_id、ip、user_agent、created_at。
9. 返回变更摘要；审计失败时按一致性策略回滚授权变更，不允许出现授权已变更但无审计证据。

### 8.4 预期涉及文件

- platform/permissions views、urls、templates、静态 JS/CSS（实际目录以 Phase 0 为准）
- apps/identity/permission_admin.py：逐步替换旧矩阵写入逻辑
- apps/identity/authorization.py：运行时统一权限服务
- apps/identity/models.py 或独立权限审计/版本模型（若设计确认需要）
- apps/core/：统一 AuditEvent/AuditService
- tests/：页面、读取 API、写入 API、CSRF、并发、审计、角色影响范围
- docs/：权限矩阵使用说明、权限点清单、变更审计规范

### 8.5 数据库变更

需要在 Phase 2 设计评审后决定：
- 可复用现有 AuditEvent 时不新增重复审计表。
- 如现有 Group 没有可安全使用的版本字段，可新增 PermissionMatrixVersion/RolePermissionSnapshot，或使用独立版本记录表。
- 若使用快照，保存每次变更前后权限码、操作者、理由、时间和版本。
- 新增 confirm token 不存明文；优先使用有过期时间的签名/一次性服务端记录。
- migration 必须考虑已有角色权限数据，禁止覆盖未纳入矩阵的权限。

### 8.6 测试要求

- GET 页面、读取 API、写入 API 分别测试未登录、非超管、超管。
- CSRF 缺失、错误 Origin、错误 HTTP 方法必须拒绝。
- 未知权限码、预留模块权限、不可编辑角色必须整体拒绝。
- 两个管理员并发保存时，后提交的旧版本请求返回冲突，不得静默覆盖。
- 高危权限无理由、无确认 token、token 过期、token 重放均拒绝。
- 正常保存后 Group.permissions 与 AuthorizationService 实际判定一致。
- before/after AuditEvent 内容完整，不包含密码、token 明文或不必要 PII。
- 保存失败时授权与审计保持事务一致。
- 角色影响范围数量与 StaffRoleAssignment 查询结果一致。

### 8.7 验收条件

- 矩阵可管理所有已纳入范围的真实跨 App 权限点。
- 每个保存结果都能看到 diff、版本和审计记录。
- 无法通过伪造请求授予未注册、预留或不允许编辑的权限。
- 并发保存不会丢权限。
- 页面勾选结果与后端执行结果一致。
- PLANNED/RETIRED 模块不显示为可授权能力。

---

## 9. Phase 4：PMS2 核心业务全链路与 API 契约验证

### 9.1 总原则

项目当前处于本地部署/测试阶段，不要求等待生产上线才开始真实请求验证。开发和测试人员应在本地 Django + 独立测试数据库中发起真实 HTTP 请求；不能只 mock 服务或凭页面展示判断成功。

所有接口字段、方法、状态码、错误码和权限必须从当前 view、serializer/DTO、service、model 和测试中核实。Swagger/OpenAPI 只能记录真实实现；未实现能力标记 planned，不得虚构接口。

登录继续记录为 Django Session HTML Form 流程，不得虚构 JSON 登录 API。

### 9.2 P0-A：顾客预约至前台接待

验证链路：

1. 顾客访问 /reserve/。
2. 页面请求 GET /api/v1/availability，读取实际房型、日期价格和库存。
3. 顾客提交 POST /api/v1/reservations。
4. 后端校验日期、房型、库存、价格、幂等键和限流。
5. 创建 Reservation、Guest/GuestPhone 及房型明细；具体关联按现有业务模型和已批准规则核验。
6. 返回 booking_no 和一次性 Booking Access Token（只显示一次，不能写日志）。
7. 员工在 /manage/reservations/ 查询到该预订及详情。
8. /checkin/ 前台 API 查询到正确抵店数据。
9. 前台排房、入住后形成 Stay、房间占用、Folio 等正确关系。
10. 收款后 Payment/Ledger 状态正确。
11. 退房后 Reservation/Stay/Folio/房态/账务状态一致。

必须检查：真实请求与响应、数据库主记录和关联记录、库存扣减/释放、审计事件、幂等重复提交、并发库存、错误输入和权限拒绝。

### 9.3 P0-B：收款、押金、退款与冲账

覆盖：
- 押金收取、普通收款、支付意图和支付回调环境门禁。
- 退款申请、审批、执行、失败、重复提交。
- 反向分录、冲账、账务更正。
- 权限分离：申请人、审批人、执行人按正式财务规则验证，不得由开发自行合并角色。
- 每笔账务记录与 Reservation/Folio/Payment/LedgerEntry/Refund 关系。
- 金额精度、币种、幂等、并发和账务不可变性。

生产环境必须阻止本地 mock 支付创建与回调；测试环境使用隔离的模拟支付，不连接真实收单渠道。

### 9.4 P0-C：夜审与营业日

覆盖：
- 夜审预览数据准确性。
- 夜审执行权限和重复执行幂等。
- 营业日结后的业务写入限制。
- 重开营业日申请、审批、执行。
- break-glass 紧急访问申请、执行、复核。
- 未到 No-Show、过租、未结账、异常房态和财务差异。
- 执行前后业务日期、账务汇总、审计事件和审批记录。

夜审执行、重开营业日、break-glass 均为高风险操作，必须有权限、理由、审计和负向测试。

### 9.5 P0-D：异常履约路径

覆盖：
- 取消预订及取消政策。
- No-Show。
- Walk-in。
- 续住。
- 换房。
- 快速退房。
- 反退房/反结账。
- 已入住订单的修改限制。
- 重复请求、状态冲突、并发操作。

每条路径验证前置状态、权限、数据库事务、库存/房态、Folio/Ledger 和审计，不只检查 HTTP 200。

### 9.6 P1：员工身份与敏感信息

覆盖：
- 员工创建、绑定、邀请、激活、停用、离职、角色分配、密码重置。
- 员工中心 /manage/staff/ 与 /staff_accounts/ 写操作迁移。
- 员工账号密码和任职状态一致性。
- 证件/电话揭示、敏感导出和审计。
- 权限矩阵修改及审计。
- 住客重复档案只读盘点；未批准前不自动按手机号合并。

### 9.7 P2：预留与退役入口

- 餐饮、停车、历史订单等 PLANNED 模块的直接 URL、API、导出和后台任务门禁。
- /register/、/booking/、/cancel_booking/ 等旧入口按已批准退役规则返回正确 302/404/410。
- 历史 hotelapp 路由不得残留可写旁路。
- 对退役入口做路由冒烟和负向写入测试，不要求对已退役功能执行完整业务流程。

### 9.8 API 契约和 Swagger/OpenAPI 交付

每个 API operation 必须逐步补齐：
- HTTP method、path、operationId、summary、tag。
- path/query/header/body 字段、类型、必填、格式、范围和示例。
- 成功响应 schema、错误响应 schema、状态码、稳定错误码。
- 认证方式、Session/Cookie、CSRF、权限码、物业范围。
- 幂等要求、限流策略、敏感字段说明。
- 业务副作用、状态转换和审计事件。
- x-contract-status：verified、partial、planned 或 deprecated 等状态。

Apifox 导入必须覆盖实际 API 路由。对 HTML 页面流程（登录、登出、强制改密）以 Web 测试用例记录，不虚构为 JSON API。对未实现的字段级契约，明确标记待核验。

### 9.9 预期涉及文件

- apps/api/urls.py、apps/api/views.py 及各业务 API 模块
- apps/core/api_contract.py
- apps/reservations/、apps/guests/、apps/inventory/、apps/billing/、apps/night_audit/、apps/identity/
- hotelapp/views.py 中仍在使用的兼容页面入口
- templates/public/booking.html、templates/pms/ 等前端页面
- OpenAPI 生成器/文档视图、接口 schema 和示例
- tests/api/、各 app tests、端到端测试
- Apifox 项目文件/环境模板（禁止提交真实密钥、Cookie 和住客数据）
- docs/PMS2接口文档与测试人员使用指南.md

### 9.10 数据库变更

业务链路测试优先使用现有模型，不为测试便利随意改业务 schema。

若审计发现必须新增字段/表，需单独提交设计：
- 说明数据所有权、唯一约束、外键、索引和事务边界。
- 说明历史数据回填、兼容窗口、回滚方案。
- 账务历史禁止删除或覆盖。
- Guest 合并策略未批准前不得新增自动合并写入。
- 测试数据库可通过 fixture/factory/管理命令重建，不得把测试数据混入生产。

### 9.11 测试要求

- 每条 P0 链路使用真实本地 HTTP 请求执行。
- Apifox 手工用例与 Python Requests + Pytest 自动化用例并行维护。
- 每个接口至少有成功、字段校验、未登录、无权限、资源不存在、状态冲突、重复请求用例。
- 高危操作补并发、幂等、CSRF、审计、角色交叉和数据库副作用验证。
- 使用测试库事务或可重置数据集；测试结果记录 commit SHA、数据库类型/版本、测试账号角色和数据集版本。
- 生成 JUnit XML；失败报告包含 request_id 和脱敏请求/响应。
- 不记录真实密码、Session Cookie、CSRF token、Booking Access Token、完整手机号或证件号。

### 9.12 验收条件

- 预约创建后，员工预订中心和前台工作台可以查询到同一笔业务数据。
- 入住、收款、退款、夜审和退房的状态、账务和审计关系正确。
- P0 所有成功与失败路径均有自动化回归用例。
- Swagger/OpenAPI 与真实路由、方法和响应一致；未完成契约明确标记。
- Apifox 可导入并在本地测试环境执行真实请求。
- 关键流程没有只靠 mock 或前端假数据通过的情况。

---

## 10. Phase 5：身份/PII、员工中心、模块门禁与旧入口治理

### 10.1 员工中心写操作迁移

- 将 /staff_accounts/ 的员工生命周期写操作逐步迁入 /manage/staff/。
- 写操作必须调用 EmployeeLifecycleService / EmployeeInvitationService 等领域服务。
- 迁移包含创建/绑定、邀请、激活、任职、角色分配、停职/离职、密码重置。
- 每项操作必须有 identity.* 权限检查、CSRF、输入校验、事务和 AuditEvent。
- 迁移完成并通过回归后，旧 /staff_accounts/ 写入口只保留兼容跳转或只读，最终按发布计划退役。
- 不得由 View 直接 create_user、改 Group、设置明文密码或绕过服务层。

### 10.2 PII 与敏感导出

- 证件揭示、手机号揭示、敏感导出使用明确的特殊权限码。
- 高风险揭示按敏感字段基线要求理由、审计和必要的二次确认。
- 列表默认脱敏；平台环境页不展示员工 PII。
- 审计记录操作者、对象、字段类型、理由、时间、request_id、IP 和 user-agent；不记录完整揭示值。
- 导出权限不能由普通 view 权限自动推导。

### 10.3 模块门禁

- 所有页面、API、导出、后台任务统一检查 ModuleRegistry 状态。
- PLANNED/RETIRED 页面应返回明确 404/410 或统一错误，不以 302 回首页伪装成功。
- API 返回稳定结构化错误。
- 建立路由门禁覆盖率测试，防止新增路由漏加门禁。
- 不允许仅靠前端隐藏菜单控制功能访问。

### 10.4 旧入口治理

按已批准业务决策核对并执行：
- /register/ 不创建 User，统一跳转 /reserve/。
- /booking/ 旧写入口冻结/跳转 /reserve/。
- /cancel_booking/ 按退役决议返回 302/410，不允许绕过 PMS2 API 写入。
- 历史 hotelapp staff/client/food/room/park/vip/order/accommodation 路由逐一核对读写状态。
- 旧页面、旧 API、后台任务、管理命令和直接 ORM 写入口一并审查。
- 删除旧代码前先检查反向引用、URL、migration、数据和审计依赖；不得直接删除历史账务或正式决策。

### 10.5 预期涉及文件

- apps/identity/models.py、services.py、authorization.py、middleware.py
- apps/guests/ 及 PII reveal 服务
- apps/api/guest_center.py、approval.py 等授权路径
- hotelapp/views.py、urls.py、decorators.py
- templates/pms/management_center.html、员工中心及相关模板
- 模块门禁与注册表定义
- migrations/（仅经审查批准后）
- tests/：员工生命周期、PII、旧入口、模块门禁和跨路由覆盖
- docs/：员工中心操作手册、敏感字段审计、旧入口退役清单

### 10.6 数据库变更

可能涉及邀请状态、PII reveal 审计、模块/角色注册表、旧记录映射等；但员工邀请与账户生命周期已由身份方案 2.0 规定，应优先复用该方案模型设计，避免重复造表。

所有 migration 必须提供：
- schema 变更说明；
- 数据回填与默认值；
- 唯一约束/外键/索引；
- 旧数据兼容；
- dry-run 或迁移前后核对；
- 备份与回滚；
- 测试环境演练结果。

### 10.7 测试要求

- 员工生命周期所有状态转换和越权操作。
- 邀请 token 一次性消费、过期、撤销、并发重放。
- PII reveal 缺权限、缺理由、重复请求、审计失败。
- /manage/staff/ 与旧 /staff_accounts/ 的行为一致性。
- PLANNED/RETIRED 页面、API、导出和任务均拒绝执行。
- 旧入口无法创建账号、预订、取消或修改历史业务数据。
- 路由清单与门禁覆盖率自动化检查。

### 10.8 验收条件

- 员工生命周期写操作有唯一服务层和统一权限。
- PII 访问与导出遵循正式敏感字段决策。
- 模块门禁覆盖所有真实执行入口。
- 旧入口无未授权写旁路。
- 历史数据、账务、审计和 migration 均保留可追溯性。

---

## 11. 全项目统一测试与发布门槛

### 11.1 每个入口的六项验收

| 检查维度 | 必须验证 |
|---|---|
| 身份认证 | 未登录、会话失效、正确身份域 |
| 权限控制 | 有权限成功、无权限 403、跨角色拒绝 |
| API 契约 | 方法、字段、状态码、错误码、响应 schema |
| 数据一致性 | 主记录、关联记录、状态机、事务和幂等 |
| 审计记录 | 成功、失败、高危动作均符合审计规则 |
| 自动化测试 | 可重复执行、可重置数据、CI 可收集结果 |

### 11.2 P0/P1/P2 风险分层

**P0：资金、不可逆操作与核心履约**
- 公开预订、库存占用、入住、Folio、收款、押金、退房。
- 退款申请、审批、执行。
- 夜审预览、执行、重开营业日、break-glass。
- 反向分录、反结账、No-Show、取消、续住、换房、快速退房、反退房。
- 权限矩阵保存和平台超管授权。

**P1：身份与敏感数据**
- 员工创建、邀请、激活、角色分配、停职/离职、密码重置。
- 证件/电话揭示、敏感导出。
- 员工中心迁移和平台/PMS 双 Session。
- 审计查询和权限影响范围。

**P2：预留与退役入口**
- 餐饮、停车、历史订单等 PLANNED 模块。
- 旧页面、旧 API、旧写入口和历史模块冒烟。
- 仅验证门禁、状态码和无副作用，不要求执行完整旧业务。

### 11.3 自动化与人工测试工具

- Apifox：导入 OpenAPI、环境变量、接口调试、测试集合和报告。
- Python Requests + Pytest：真实 HTTP 回归、权限矩阵、幂等/并发、数据库副作用辅助断言。
- Django Test Client：服务层和视图级快速测试。
- 浏览器端到端测试：登录、平台布局、权限矩阵、顾客预订、前台履约。
- 所有测试只使用本地或专用测试环境，不使用生产真实住客数据和真实支付凭据。

### 11.4 CI 与发布门槛

- 所有相关 Django tests 通过；失败/跳过必须解释。
- 迁移检查通过；不得存在未审查的 schema drift。
- 权限点映射、路由门禁覆盖率、OpenAPI 路由覆盖检查通过。
- P0 自动化用例通过。
- 高危权限变更、资金和夜审操作审计检查通过。
- 文档、Swagger、Apifox 测试集合与代码同一提交更新。
- 主分支每个阶段保留独立提交和可回滚节点；禁止未验收的大批量覆盖式提交。

---

## 12. 排期建议与交付物

排期以工作量评估后为准，不预先虚构人日。建议按可验收任务拆分：

| 批次 | 任务包 | 主要交付物 | 阻塞关系 |
|---|---|---|---|
| 0.1 | 路由与身份域审计 | 入口清单、Session/认证矩阵 | 无 |
| 0.2 | 权限点与角色审计 | 跨 App 权限点表、角色差异报告 | 0.1 |
| 0.3 | 业务数据流审计 | 预约至履约、财务、夜审数据流图 | 0.1 |
| 0.4 | 测试基线 | 测试报告、测试账号/数据方案、OpenAPI 差异 | 0.1-0.3 |
| 1.1 | 平台身份不变量 | 初始化/自检命令、授权策略 | Phase 0 |
| 1.2 | 平台路由和布局 | /platform/、权限页迁移 | 1.1 |
| 1.3 | /system/ 拆分 | PMS 个人页、平台环境页 | 1.1-1.2 |
| 2.1 | RBAC 注册表设计 | Module/Role/SpecialPermission 清单 | Phase 0 |
| 2.2 | 权限映射和迁移方案 | 旧权限到新权限映射、回滚计划 | 2.1 |
| 3.1 | 权限读取 API/页面 | 角色、权限、影响范围 | Phase 1-2 |
| 3.2 | 权限保存安全 | diff、版本、二次确认、AuditEvent | 3.1 |
| 3.3 | 授权执行点迁移 | billing/identity/night_audit 等真实执行点 | Phase 2-3 |
| 4.1 | 预约/前台链路 | 真实请求和数据一致性测试 | Phase 0；授权最终验收依赖 Phase 3 |
| 4.2 | 收款/退款链路 | 账务与审批测试 | 4.1、Phase 3 |
| 4.3 | 夜审/异常链路 | 夜审、重开、No-Show、反结账测试 | Phase 3 |
| 4.4 | Swagger/Apifox | 完整接口契约和测试集合 | 各 API 契约核验 |
| 5.1 | 员工中心迁移 | 生命周期写操作统一入口 | Phase 1-3 |
| 5.2 | PII/模块门禁 | 敏感权限与路由覆盖测试 | Phase 2-3 |
| 5.3 | 旧入口退役 | 旧写路径封闭报告 | Phase 4/5 对应业务验收 |

每个任务包必须有：负责人、代码范围、依赖、估算、测试命令、验收证据、回滚方式。没有负责人和验收标准的任务不得进入开发中状态。

## 13. 变更治理与禁止事项

1. 开发以最新 main 为基线；开始任务前记录 SHA，提交后记录 commit SHA。
2. 直接在 main 开发时按阶段拆分提交；每个提交只包含一个可说明、可回滚的任务包。
3. 未经负责人确认，不得变更角色职责、敏感字段边界、财务审批、退款、夜审和 Guest 合并规则。
4. 不得把 is_superuser 解释为自动拥有 PMS 全部业务权限。
5. 不得将 User.groups 作为 PMS 业务授权兜底。
6. 不得只修改权限矩阵 UI 而不接入真实后端执行点。
7. 不得把 PLANNED 模块权限授予为有效权限。
8. 不得使用 AuditEvent 作为高频限流计数器。
9. 不得把 Session Cookie、CSRF token、Booking Access Token、密码或真实住客 PII 写入日志、文档或仓库。
10. 不得使用生产库执行破坏性测试、退款、夜审、反结账或账号清理。
11. 不得删除历史 migration、账务记录、审计事件或正式决策文档来掩盖现状差异。
12. 数据库变更必须有迁移前快照、数据核对、备份和回滚方案。
13. Swagger/OpenAPI 不得描述不存在的接口；字段未核验时必须标注状态。
14. 测试通过必须附环境、数据库、测试命令、测试结果和代码 SHA；历史测试不能冒充当前通过。

## 14. 待负责人确认的边界事项

以下事项不阻塞 Phase 0，但在对应开发开始前必须明确：

| 事项 | 当前建议 | 决策时间点 |
|---|---|---|
| 平台超管双身份人员 | 优先两个账号：平台技术账号 + PMS 业务账号 | Phase 1 实施前 |
| /permissions/ 旧路径 | 兼容跳转到 /platform/permissions/，最终按访问日志决定退役 | Phase 1 |
| 注册表持久化方式 | 先做代码注册表和权限清单，确认动态管理需求后再决定数据库模型 | Phase 2 |
| 权限矩阵版本/快照 | 优先复用 AuditEvent；若并发版本无法安全表达再新增模型 | Phase 3 |
| Guest 去重/合并 | 不自动按手机号或姓名合并，待业务单独决策 | 未来 Guest 治理专项 |
| 平台页是否需要更多角色 | 本期仅平台超管；不引入平台操作员角色 | 若未来出现需求再立项 |
| 旧模块最终删除时间 | 先封闭写入口、保留数据与审计，删除需另行评审 | Phase 5 |

## 15. 最终验收定义

本方案全部完成必须同时满足：

- 平台身份域与 PMS 员工身份域边界清晰，双 Session 独立且测试通过。
- 平台超管身份不变量在初始化、登录、页面、API 和后台操作中一致。
- /system/ 已拆分，员工个人信息与平台环境信息不再混用。
- 跨 App RBAC 权限点全部登记，矩阵展示与后端执行一致。
- 权限保存具有版本控制、差异展示、风险确认和数据库审计。
- PLANNED/RETIRED 模块无法通过页面、API、导出、任务或伪造请求执行。
- 顾客预约创建的数据能够被员工预订中心和前台工作台读取，并在入住、账务、退房后保持关系一致。
- 收款、退款、夜审、反向操作和异常履约路径均有授权、数据副作用和审计验证。
- Swagger/OpenAPI 真实反映接口；Apifox 可导入；Requests + Pytest 可重复执行。
- 员工生命周期、PII reveal、旧入口治理符合已批准决策。
- 所有 P0 测试通过，P1/P2 缺陷有明确处置和风险接受记录。
- 文档、代码、迁移和测试结果对应同一提交基线。

**本方案的执行顺序总结：先盘清现状，再隔离平台身份；先定义真实权限点，再重构权限矩阵；同时以真实 HTTP 请求验证 PMS2 业务数据流，最后治理员工生命周期、敏感信息和旧入口。**
