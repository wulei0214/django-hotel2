// Semantic authoring pass 1: apps/api/* and apps/billing/*
// Value format: [summary(zh), [tags]]
export const FILES = {
  'apps/api/__init__.py': ['apps.api 包标记文件，声明 API 应用包。', ['package', 'barrel']],
  'apps/api/approval.py': [
    '审批中心 API：查询审批单、提交审批请求、执行通过与驳回，供夜审重开、退款等高危动作走二次授权。',
    ['api-handler', 'approval', 'workflow'],
  ],
  'apps/api/apps.py': ['Django AppConfig，注册 apps.api 应用。', ['django', 'app-config']],
  'apps/api/audit_center.py': [
    '审计中心 API：分页查询审计事件、查看事件详情与汇总、列出可审计动作，并对 PII 字段做脱敏。',
    ['api-handler', 'audit', 'pii-masking'],
  ],
  'apps/api/billing_center.py': [
    '账务中心 API：账单（folio）列表与详情、分录/收款/退款/退款负债、收银班次、手工收款与冲销等财务接口。',
    ['api-handler', 'billing', 'finance', 'core-domain'],
  ],
  'apps/api/dashboard.py': [
    '首页看板数据接口：汇总经营指标、图表序列与告警提示，供管理端首页展示。',
    ['api-handler', 'dashboard', 'reporting'],
  ],
  'apps/api/front_desk.py': [
    '前台工作台 API：到店/离店/在店列表、房间与可分配房查询、排房、收款、账单摘要与在店异常释放。',
    ['api-handler', 'front-desk', 'core-domain'],
  ],
  'apps/api/guest_center.py': [
    '客人中心 API：客人检索与 360 视图、时间线、证件/手机号按原因解密、会员与房价日历查询。',
    ['api-handler', 'guest', 'pii-masking'],
  ],
  'apps/api/housekeeping_center.py': [
    '客房中心 API：房态看板、房态流转与维修单状态变更，联动库存可用性。',
    ['api-handler', 'housekeeping', 'inventory'],
  ],
  'apps/api/management/commands/export_openapi_schema.py': [
    '管理命令：把运行时生成的 OpenAPI 文档导出为 JSON 文件，供前端与契约测试比对。',
    ['management-command', 'openapi', 'tooling'],
  ],
  'apps/api/night_audit.py': [
    '夜审中心 API：夜审执行、营业日详情、重开申请/审批、破玻璃请求/执行/复核等接口。',
    ['api-handler', 'night-audit', 'approval'],
  ],
  'apps/api/notification_center.py': [
    '通知中心 API：待办/通知列表、未读数、标记已读、全部已读与手动刷新派生待办。',
    ['api-handler', 'notification', 'todo'],
  ],
  'apps/api/openapi_schema.py': [
    'OpenAPI 3 文档构建器：以手工描述覆盖全部 PMS2 路由，生成 paths/components 供契约测试与前端使用（超大单文件）。',
    ['openapi', 'contract', 'documentation', 'generated'],
  ],
  'apps/api/pages.py': [
    '页面渲染视图：公开预订页与管理中心（多中心统一落页）的服务端渲染入口。',
    ['view', 'page', 'server-rendered'],
  ],
  'apps/api/report_center.py': [
    '报表中心 API：出租率、ADR、收入、客人、客房等经营报表，含区间解析与元数据。',
    ['api-handler', 'reporting', 'analytics'],
  ],
  'apps/api/reservation_center.py': [
    '预订中心 API：预订列表、详情与审计时间线查询。',
    ['api-handler', 'reservation'],
  ],
  'apps/api/staff_center.py': [
    '员工中心 API：员工检索与详情、员工-物业成员关系（membership）的增改与状态/等级变更。',
    ['api-handler', 'staff', 'identity'],
  ],
  'apps/api/test_approval.py': [
    '审批 API 回归测试：审批列表权限、请求白名单、通过/驳回决策。',
    ['test', 'approval', 'api-contract'],
  ],
  'apps/api/test_audit_center.py': [
    '审计中心回归测试：权限、分页、过滤、多物业隔离、详情、PII 保护、汇总与保留期说明。',
    ['test', 'audit', 'api-contract'],
  ],
  'apps/api/test_auth_hardening.py': [
    '鉴权加固测试：注册/预订冻结、登录错误统一、密码策略、模拟支付闸门、登录限流与公开预订防滥用。',
    ['test', 'security', 'auth'],
  ],
  'apps/api/test_billing_center.py': [
    '账务中心回归测试（超大）：读写权限、退款三方一致性、权限迁移矩阵、CSRF、退款负债完整性等。',
    ['test', 'billing', 'api-contract'],
  ],
  'apps/api/test_center_template_regressions.py': [
    '中心页面模板回归测试：校验五大中心模板的渲染与关键元素。',
    ['test', 'template', 'ui'],
  ],
  'apps/api/test_dup_convergence_guards.py': [
    '重复收敛守卫测试：防止重复预订/重复入账等重复写入路径回归。',
    ['test', 'guard', 'idempotency'],
  ],
  'apps/api/test_e2e_flows.py': [
    '端到端流程测试：押金-收款-退款-冲正、夜审营业日、异常履约等完整链路（超大文件）。',
    ['test', 'e2e', 'finance'],
  ],
  'apps/api/test_finance_route_regressions.py': [
    '旧财务路由退役测试：确认旧财务入口已下线。',
    ['test', 'route-migration'],
  ],
  'apps/api/test_five_centers_closure.py': [
    '五大中心闭环测试：员工离职、客人 360、会员状态、客房库存、收银班次与对账一致性。',
    ['test', 'e2e', 'closure'],
  ],
  'apps/api/test_housekeeping_center.py': [
    '客房中心回归测试：读接口、房态流转、维修单、页面与注册表。',
    ['test', 'housekeeping'],
  ],
  'apps/api/test_night_audit_center.py': [
    '夜审中心回归测试：读接口、阻断规则与页面。',
    ['test', 'night-audit'],
  ],
  'apps/api/test_notification_center.py': [
    '通知中心回归测试：待办派生、刷新幂等与自动解决、接口与页面。',
    ['test', 'notification'],
  ],
  'apps/api/test_openapi_schema.py': [
    'OpenAPI 契约测试：文档结构合法性、契约状态、路由同步、错误信封、权限注解、幂等与字段一致性。',
    ['test', 'openapi', 'contract'],
  ],
  'apps/api/test_pagination_contract.py': [
    '分页契约测试：分页参数解析、稳定排序、多端点抽样、跨物业过滤。',
    ['test', 'pagination', 'contract'],
  ],
  'apps/api/test_public_booking_lifecycle.py': [
    '公开预订生命周期测试：从查房、下单、支付到取消/释放库存的完整链路。',
    ['test', 'booking', 'e2e'],
  ],
  'apps/api/test_report_center.py': [
    '报表中心回归测试：权限、区间校验、元数据、各报表数值与空数据、页面。',
    ['test', 'reporting'],
  ],
  'apps/api/test_security.py': [
    '安全回归测试：物业隔离与 PII 保护。',
    ['test', 'security'],
  ],
  'apps/api/test_whitebox_api.py': [
    'API 白盒测试：非对象 JSON、参数校验、脱敏、公开物业守卫、越权访问、登录态与契约一致性。',
    ['test', 'whitebox', 'security'],
  ],
  'apps/api/tests.py': [
    'API 基础测试集：公开预订接口、前台读/写接口与前台工作台页面。',
    ['test', 'api'],
  ],
  'apps/api/urls.py': [
    'API 路由表：注册五大中心、公开预订、页面与平台入口的 URL 到视图映射。',
    ['routing', 'urls', 'entry-point'],
  ],
  'apps/api/views.py': [
    '公开/兼容 API 视图：房量查询、预订创建与详情、支付意图与回调、取消与限流处理。',
    ['api-handler', 'booking', 'payment'],
  ],

  'apps/billing/__init__.py': ['apps.billing 包标记文件。', ['package', 'barrel']],
  'apps/billing/admin.py': [
    'Django Admin 配置：账单、分录、收款、退款、审批记录与退款负债的后台管理视图。',
    ['admin', 'billing', 'backoffice'],
  ],
  'apps/billing/approval_registry.py': [
    '审批动作注册表：登记需二次授权的高危动作（退款、冲销等）及其校验器。',
    ['approval', 'registry', 'policy'],
  ],
  'apps/billing/apps.py': ['Django AppConfig，注册 apps.billing 应用。', ['django', 'app-config']],
  'apps/billing/dec02.py': [
    'DEC-02 账务决策实现：调账申请/审批/执行、例外释放、负债净额快照。',
    ['billing', 'approval', 'policy'],
  ],
  'apps/billing/liabilities.py': [
    '退款负债登记：为退款建立负债事实记录，保证退款与负债一一对应。',
    ['billing', 'liability', 'invariant'],
  ],
  'apps/billing/management/__init__.py': ['billing 管理命令包标记。', ['package']],
  'apps/billing/management/commands/__init__.py': ['billing 管理命令子包标记。', ['package']],
  'apps/billing/management/commands/expire_pending_refunds.py': [
    '管理命令：把超时未完成的退款标记为过期，释放负债占用。',
    ['management-command', 'refund', 'scheduler'],
  ],
  'apps/billing/models.py': [
    '账务领域模型：账单/分录/收款/退款/退款负债/收银班次/调账/支付意图等不可变财务事实（超大模型文件）。',
    ['data-model', 'billing', 'immutable-facts', 'core-domain'],
  ],
  'apps/billing/night_audit.py': [
    '夜审账务服务：营业日结账、收入确认、重开与破玻璃流程的领域实现。',
    ['service', 'night-audit', 'billing'],
  ],
  'apps/billing/payment_intents.py': [
    '支付意图生命周期：创建/延时/记录渠道事件/确认成功/过期，以及补偿任务领取与完成。',
    ['service', 'payment', 'state-machine'],
  ],
  'apps/billing/reconciliation.py': [
    '账单对账扫描：比对账单事实与分录，产出对账运行与差异记录。',
    ['service', 'reconciliation', 'billing'],
  ],
  'apps/billing/services.py': [
    '账务基础服务：模拟支付意图、支付回调处理、分录冲销与预订未结支付意图清理。',
    ['service', 'billing', 'payment'],
  ],
  'apps/billing/services_v2.py': [
    '账务核心服务（v2）：收款、退款、冲销、调账、班次等业务入口与审批编排（超大服务类）。',
    ['service', 'billing', 'core-domain', 'approval'],
  ],
  'apps/billing/test_approval_registry.py': [
    '审批注册表测试：动作目录完整性、白名单校验与服务层拦截。',
    ['test', 'approval'],
  ],
  'apps/billing/test_break_glass.py': [
    '破玻璃流程测试：紧急越权操作的申请、执行与审计。',
    ['test', 'break-glass', 'security'],
  ],
  'apps/billing/test_dec02.py': [
    'DEC-02 决策测试：调账与例外释放的申请-审批-执行链路。',
    ['test', 'billing', 'policy'],
  ],
  'apps/billing/test_night_audit_reopen.py': [
    '夜审重开测试：重开申请与审批终态后的再申请约束。',
    ['test', 'night-audit'],
  ],
  'apps/billing/test_payment_dec02.py': [
    '支付决策测试：支付意图与补偿任务。',
    ['test', 'payment'],
  ],
  'apps/billing/test_whitebox_services.py': [
    '账务白盒测试（超大）：资金守卫、冲销、审批并发、押金、退款三方执行、不可变事实与数据库约束。',
    ['test', 'whitebox', 'billing', 'concurrency'],
  ],
  'apps/billing/tests.py': [
    '账务单元测试：模拟支付、审批记录、夜审服务与手工收款。',
    ['test', 'billing'],
  ],
};

// 领域类节点摘要（仅核心领域/服务类）
export const CLASSES = {
  'apps/billing/models.py': {
    ApprovalRecord: '审批记录：高危动作的申请、决策与留痕，写入后不可变。',
    Folio: '账单主实体：一个预订/住店期间的消费与结算容器。',
    FolioTransfer: '账单转账记录：把账单项在账单之间转移。',
    LedgerEntry: '账务分录：不可变的借贷事实，构成财务账本。',
    Payment: '收款记录：现金/POS/模拟渠道的实际到账事实。',
    Refund: '退款记录：退款申请与执行状态，关联负债与审批。',
    RefundLiability: '退款负债：待执行退款形成的负债事实。',
    CashierShift: '收银班次：收银员当班期间的现金交班与对账单元。',
    CashierShiftMovement: '班次现金流水：班次内现金收支明细。',
    ChargeLine: '消费行：账单上的单项收费。',
    TaxLine: '税费行：按税则拆分的税额明细。',
    BillingAdjustment: '调账单：需要审批的账务调整申请与执行结果。',
    CheckoutExceptionRelease: '离店例外释放：余额不足/异常离店的审批放行记录。',
    PaymentIntent: '支付意图：渠道支付的幂等意图与状态机。',
    ProviderTransaction: '渠道交易：支付渠道回调记录。',
    PaymentCompensationTask: '支付补偿任务：失败支付的补偿重试工作单元。',
    FolioReconciliationRun: '账单对账运行：一次对账扫描的执行记录。',
    FolioReconciliationDiff: '对账差异：扫描发现的账实差异明细。',
    ImmutableFinancialFactQuerySet: '不可变财务事实查询集：禁止 update/delete。',
  },
  'apps/billing/services_v2.py': {
    ApprovalService: '审批编排服务：按注册表校验并执行高危动作审批。',
    BillingService: '账务业务服务：收款、退款、冲销、调账与班次的核心实现。',
  },
  'apps/billing/night_audit.py': {
    NightAuditService: '夜审服务：营业日结账、收入结转、重开与破玻璃的账务处理。',
  },
  'apps/billing/approval_registry.py': {
    ApprovalActionSpec: '审批动作规格：动作编码、所需权限与实体校验器。',
    ApprovalActionRegistry: '审批动作注册表：登记与查找所有需审批的动作。',
  },
};
