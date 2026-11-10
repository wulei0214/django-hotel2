// Semantic authoring pass 2: apps/core, apps/guests, apps/identity, apps/inventory, apps/reservations
export const FILES = {
  'apps/core/__init__.py': ['apps.core 包标记文件。', ['package', 'barrel']],
  'apps/core/admin.py': [
    '核心后台管理：物业、审计事件、遗留记录关联与破玻璃记录的 Admin 配置（只读为主）。',
    ['admin', 'audit', 'backoffice'],
  ],
  'apps/core/api_contract.py': [
    'API 契约工具库：统一响应信封、错误码、分页参数解析、稳定排序、分页器与幂等装饰器。',
    ['utility', 'api-contract', 'pagination', 'idempotency'],
  ],
  'apps/core/apps.py': ['Django AppConfig，注册 apps.core 应用。', ['django', 'app-config']],
  'apps/core/labels.py': [
    '领域中文标签字典：状态、角色、动作等枚举的中文显示名，供接口与模板复用。',
    ['utility', 'i18n', 'labels'],
  ],
  'apps/core/management/__init__.py': ['core 管理命令包标记。', ['package']],
  'apps/core/management/commands/__init__.py': ['core 管理命令子包标记。', ['package']],
  'apps/core/management/commands/check_mysql_append_only_guards.py': [
    '管理命令：检查 MySQL 是否已安装审计表只追加（触发器/权限）守卫。',
    ['management-command', 'mysql', 'integrity'],
  ],
  'apps/core/management/commands/install_mysql_append_only_guards.py': [
    '管理命令：在 MySQL 上安装审计表只追加守卫（触发器），阻断 UPDATE/DELETE。',
    ['management-command', 'mysql', 'integrity'],
  ],
  'apps/core/models.py': [
    '核心模型：物业、审计事件（不可变）、营业日、破玻璃记录、夜审运行、幂等记录、状态流转、限流计数与通知。',
    ['data-model', 'audit', 'core-domain'],
  ],
  'apps/core/rate_limit.py': [
    '限流实现：基于计数表的滑动窗口限流检查，用于登录与公开接口防刷。',
    ['utility', 'rate-limit', 'security'],
  ],
  'apps/core/services_notifications.py': [
    '运营待办派生服务：按业务条件汇总活跃待办并刷新通知记录（幂等）。',
    ['service', 'notification', 'todo'],
  ],
  'apps/core/sql/audit_immutability.sql': [
    '审计不可变 SQL 守卫：在数据库层为审计表建立只追加约束与触发器。',
    ['database', 'audit', 'integrity'],
  ],
  'apps/core/test_audit_immutability.py': [
    '审计不可变测试：验证审计事件无法被更新或删除。',
    ['test', 'audit', 'integrity'],
  ],
  'apps/core/tests.py': ['核心模型测试：物业模型基础行为。', ['test', 'model']],

  'apps/guests/__init__.py': ['apps.guests 包标记文件。', ['package', 'barrel']],
  'apps/guests/admin.py': ['客人后台管理：只读 Admin 混入与客人管理视图。', ['admin', 'guest', 'read-only']],
  'apps/guests/apps.py': ['Django AppConfig，注册 apps.guests 应用。', ['django', 'app-config']],
  'apps/guests/models.py': [
    '客人领域模型：客人主档、手机号（脱敏存储）、证件、会员与会籍等级历史。',
    ['data-model', 'guest', 'pii', 'core-domain'],
  ],
  'apps/guests/services.py': [
    '客人服务：建客原子性、证件/手机号解密（需原因并留痕）、会员等级与状态变更。',
    ['service', 'guest', 'pii-masking'],
  ],
  'apps/guests/templatetags/__init__.py': ['guests 模板标签包标记。', ['package']],
  'apps/guests/test_whitebox_services.py': [
    '客人服务白盒测试：脱敏纪律与建客原子性。',
    ['test', 'whitebox', 'guest'],
  ],

  'apps/identity/__init__.py': ['apps.identity 包标记文件。', ['package', 'barrel']],
  'apps/identity/admin.py': [
    '身份后台管理：员工档案、物业成员关系与角色分配的只读 Admin。',
    ['admin', 'identity', 'read-only'],
  ],
  'apps/identity/apps.py': ['Django AppConfig，注册 apps.identity 应用。', ['django', 'app-config']],
  'apps/identity/authorization.py': [
    '授权服务：按角色/权限编码判定员工是否具备某动作权限，是权限体系的判定入口。',
    ['service', 'authorization', 'security'],
  ],
  'apps/identity/context_processors.py': [
    '模板上下文处理器：向模板注入当前员工、角色、权限与物业上下文。',
    ['middleware', 'template-context'],
  ],
  'apps/identity/decorators.py': [
    '权限装饰器：员工身份校验与权限校验的视图装饰器。',
    ['utility', 'decorator', 'authorization'],
  ],
  'apps/identity/employee_number.py': [
    '员工编号服务：生成工号、离职再入职复用档案并记录审计。',
    ['service', 'identity', 'audit'],
  ],
  'apps/identity/invitation_services.py': [
    '员工邀请服务：生成一次性邀请令牌、校验、激活建号与失效管理。',
    ['service', 'identity', 'invitation', 'security'],
  ],
  'apps/identity/invitation_views.py': [
    '邀请激活页面与提交处理：令牌校验、限流、密码策略与激活建号。',
    ['view', 'identity', 'invitation'],
  ],
  'apps/identity/management/__init__.py': ['identity 管理命令包标记。', ['package']],
  'apps/identity/management/commands/__init__.py': ['identity 管理命令子包标记。', ['package']],
  'apps/identity/management/commands/bind_staff_user.py': [
    '管理命令：把已有 Django 用户绑定到员工档案/成员关系。',
    ['management-command', 'identity', 'migration'],
  ],
  'apps/identity/management/commands/expire_invitations.py': [
    '管理命令：把过期未使用的员工邀请标记为失效。',
    ['management-command', 'invitation', 'scheduler'],
  ],
  'apps/identity/management/commands/platform_superadmin_check.py': [
    '管理命令：检查平台超管账号的安全状态与配置。',
    ['management-command', 'platform', 'security'],
  ],
  'apps/identity/middleware.py': [
    '物业上下文中间件：解析当前请求所属物业并注入请求上下文，隔离多物业数据。',
    ['middleware', 'property-context', 'multi-tenant'],
  ],
  'apps/identity/models.py': [
    '身份领域模型：员工档案、物业成员关系、角色分配与员工邀请。',
    ['data-model', 'identity', 'core-domain'],
  ],
  'apps/identity/permission_admin.py': ['权限注册表的后台只读展示配置。', ['admin', 'permission']],
  'apps/identity/permission_center.py': [
    '权限中心 API：权限矩阵读写、版本号、确认令牌校验与未知权限拒绝。',
    ['api-handler', 'permission', 'security'],
  ],
  'apps/identity/platform.py': [
    '平台管理入口：平台超管校验、系统信息页与平台登出。',
    ['view', 'platform', 'superadmin'],
  ],
  'apps/identity/registry.py': [
    '权限/模块注册表：定义九类角色、模块权限编码与计划中模块的开关校验（含注册表合法性校验）。',
    ['registry', 'permission', 'module-gate'],
  ],
  'apps/identity/roles.py': ['角色常量定义（九类角色编码）。', ['constants', 'roles']],
  'apps/identity/services.py': [
    '员工生命周期服务：入职、调动、离职、复职及成员关系与角色分配变更。',
    ['service', 'identity', 'lifecycle'],
  ],
  'apps/identity/session_middleware.py': [
    '会话/Cookie 隔离中间件：为 PMS、平台门户与平台 Admin 拆分独立 Cookie 会话，防止串号。',
    ['middleware', 'session', 'security'],
  ],
  'apps/identity/staff_center_views.py': [
    '员工中心页面视图与动作：建号、开通、分配角色、停用、重置密码与邀请。',
    ['view', 'staff', 'identity'],
  ],
  'apps/identity/test_cookie_isolation.py': [
    'Cookie 隔离测试：会话规格分类、登录一致性、非法 Cookie、登出、遗留迁移与 CSRF 隔离。',
    ['test', 'session', 'security'],
  ],
  'apps/identity/test_invitations.py': [
    '邀请测试：令牌安全、一次性消费、生命周期、密码策略、审计最小化与页面/接口。',
    ['test', 'invitation', 'security'],
  ],
  'apps/identity/test_module_gates.py': [
    '模块闸门测试：PII 解密原因强制、敏感导出边界、计划模块闸门与旧入口无绕过。',
    ['test', 'module-gate', 'security'],
  ],
  'apps/identity/test_permission_center.py': [
    '权限中心测试：读接口、写守卫、禁用码、版本号、确认令牌、保存生效与页面。',
    ['test', 'permission'],
  ],
  'apps/identity/test_platform.py': [
    '平台管理测试：访问控制、CSRF、上下文噪声、旧权限重定向、系统页拆分与超管检查命令。',
    ['test', 'platform'],
  ],
  'apps/identity/test_registry.py': [
    '注册表测试：一致性、计划模块授权守卫、维护/冲销权限、模块闸门与角色注册表。',
    ['test', 'registry'],
  ],
  'apps/identity/test_staff_center.py': [
    '员工中心测试：访问控制、写入动作、旧员工账号退役、安全与离职重置守卫。',
    ['test', 'staff'],
  ],
  'apps/identity/tests.py': [
    '身份基础测试：授权服务、员工生命周期、Admin 只读、九角色跨模块权限与拆分会话。',
    ['test', 'identity', 'authorization'],
  ],

  'apps/inventory/__init__.py': ['apps.inventory 包标记文件。', ['package', 'barrel']],
  'apps/inventory/admin.py': ['库存后台管理：房型、房间、房价计划、日房价与房量库存的只读 Admin。', ['admin', 'inventory']],
  'apps/inventory/apps.py': ['Django AppConfig，注册 apps.inventory 应用。', ['django', 'app-config']],
  'apps/inventory/management/__init__.py': ['inventory 管理命令包标记。', ['package']],
  'apps/inventory/management/commands/__init__.py': ['inventory 管理命令子包标记。', ['package']],
  'apps/inventory/management/commands/sync_inventory_calendar.py': [
    '管理命令：按日历批量同步房量库存（房型-日期维度）。',
    ['management-command', 'inventory', 'scheduler'],
  ],
  'apps/inventory/models.py': [
    '库存领域模型：房型、房间、房价计划、日房价、房量库存、客房任务、维修单、查房、对账运行与差异。',
    ['data-model', 'inventory', 'housekeeping', 'core-domain'],
  ],
  'apps/inventory/reconciliation.py': [
    '库存对账：扫描房态/库存不一致，支持申请、审批、执行修复与忽略差异。',
    ['service', 'reconciliation', 'inventory', 'approval'],
  ],
  'apps/inventory/services.py': [
    '库存服务：房态流转、维修联动、可用房量计算、预订占房/确认/释放与日房价调整。',
    ['service', 'inventory', 'availability', 'core-domain'],
  ],
  'apps/inventory/test_reconciliation.py': ['库存对账测试：库存差异扫描与账单对账。', ['test', 'reconciliation']],
  'apps/inventory/test_whitebox_services.py': [
    '库存白盒测试：日房价边界、房态汇总时区与维修-库存联动。',
    ['test', 'whitebox', 'inventory'],
  ],
  'apps/inventory/tests.py': ['库存单元测试：可用量与日房价调整。', ['test', 'inventory']],

  'apps/reservations/__init__.py': ['apps.reservations 包标记文件。', ['package', 'barrel']],
  'apps/reservations/admin.py': ['预订后台管理：预订、住店、预订房间与住客关联的只读 Admin。', ['admin', 'reservation']],
  'apps/reservations/apps.py': ['Django AppConfig，注册 apps.reservations 应用。', ['django', 'app-config']],
  'apps/reservations/front_desk_services.py': [
    '前台住店服务：排房、办理入住、手工收款、办理离店（含账单结算）。',
    ['service', 'front-desk', 'core-domain'],
  ],
  'apps/reservations/lifecycle_services.py': [
    '住店生命周期服务：续住、换房、标记未到（no-show）与押金收取。',
    ['service', 'lifecycle', 'reservation'],
  ],
  'apps/reservations/management/__init__.py': ['reservations 管理命令包标记。', ['package']],
  'apps/reservations/management/commands/__init__.py': ['reservations 管理命令子包标记。', ['package']],
  'apps/reservations/management/commands/expire_reservation_holds.py': [
    '管理命令：过期未确认的预订占房并释放库存。',
    ['management-command', 'reservation', 'scheduler'],
  ],
  'apps/reservations/models.py': [
    '预订领域模型：预订、预订房间、住店、换房段、未到记录、住客、库存占用、取消/押金/早离政策与预订访问令牌。',
    ['data-model', 'reservation', 'core-domain'],
  ],
  'apps/reservations/services.py': [
    '预订服务：创建预订（锁库存、校验冲突）、取消待确认预订。',
    ['service', 'reservation', 'inventory'],
  ],
  'apps/reservations/test_six_rules.py': [
    '六项业务规则测试：押金政策、再入职、工号与早离规则。',
    ['test', 'policy'],
  ],
  'apps/reservations/test_whitebox_services.py': [
    '预订服务白盒测试：离店账单范围、生命周期账单范围、金额边界与续住边界。',
    ['test', 'whitebox', 'reservation'],
  ],
  'apps/reservations/tests.py': [
    '预订测试集：预订创建与前台住店全生命周期（超大测试文件）。',
    ['test', 'reservation', 'front-desk'],
  ],
  'apps/reservations/walkin.py': [
    '散客入住（walk-in）：早离退费计算与无预订直接入住的建单流程。',
    ['service', 'walk-in', 'front-desk'],
  ],
};

export const CLASSES = {
  'apps/core/models.py': {
    Property: '物业（酒店门店）主实体，多物业隔离的根。',
    AuditEvent: '审计事件：只追加的不可变审计日志。',
    LegacyRecordLink: '遗留记录关联：新旧模型 id 的迁移映射。',
    BusinessDate: '营业日：夜审推进的营业日游标。',
    BreakGlassRecord: '破玻璃记录：紧急越权操作的申请/执行/复核。',
    NightAuditRun: '夜审运行记录：一次夜审的执行结果。',
    IdempotencyRecord: '幂等记录：请求去重的事实存储。',
    StatusTransition: '状态流转记录：订单/房态等状态变更留痕。',
    RateLimitCounter: '限流计数：滑动窗口限流的状态表。',
    Notification: '通知/待办实体：运营待办与消息。',
    NotificationRead: '通知已读状态。',
  },
  'apps/identity/models.py': {
    StaffProfile: '员工档案：工号、姓名、在职状态与账号绑定。',
    PropertyStaffMembership: '物业成员关系：员工在某物业的岗位与状态。',
    StaffRoleAssignment: '角色分配：把 Django 组/角色授予成员关系。',
    Invitation: '员工邀请：一次性令牌与有效期。',
  },
  'apps/identity/services.py': {
    EmployeeLifecycleService: '员工生命周期服务：入职、调动、离职、复职的业务实现。',
  },
  'apps/identity/authorization.py': {
    AuthorizationService: '授权判定服务：角色/权限编码到动作许可的映射。',
  },
  'apps/identity/session_middleware.py': {
    SessionCookieSpec: '会话 Cookie 规格：名称、域、路径与安全属性。',
    SplitSessionMiddleware: '拆分会话中间件：按入口选择独立会话 Cookie。',
  },
  'apps/identity/invitation_services.py': {
    EmployeeInvitationService: '邀请服务：创建、校验与消费邀请令牌。',
  },
  'apps/guests/models.py': {
    Guest: '客人主档：姓名、联系方式与标签。',
    GuestPhone: '客人手机号：脱敏存储与按原因解密。',
    GuestDocument: '客人证件：证件类型与脱敏号码。',
    GuestMembership: '会员会籍：等级与状态。',
    MembershipLevelHistory: '会籍等级变更历史。',
  },
  'apps/guests/services.py': {
    GuestService: '客人服务：建客与 PII 解密。',
    GuestMembershipService: '会员服务：等级与状态变更。',
  },
  'apps/inventory/models.py': {
    RoomType: '房型：房型编码、名称与基础容量。',
    Room: '房间：物理房号、房型与当前房态。',
    RatePlan: '房价计划：价格策略与包含项。',
    DailyRate: '日房价：房型-日期的价格。',
    RoomTypeInventory: '房量库存：房型-日期的可售房量。',
    HousekeepingTask: '客房任务：清扫/查房任务与状态。',
    MaintenanceTicket: '维修单：房间维修与影响房态。',
    RoomInspection: '查房记录：客房检查结果。',
    InventoryReconciliationRun: '库存对账运行记录。',
    InventoryDiscrepancy: '库存差异明细。',
  },
  'apps/reservations/models.py': {
    Reservation: '预订主实体：来源、状态、日期与金额。',
    ReservationRoom: '预订房间：预订与房间/房型的关联行。',
    Stay: '住店：入住后的在店事实。',
    StayRoomSegment: '住店房间段：换房产生的时间段。',
    NoShowRecord: '未到记录（no-show）。',
    ReservationGuest: '预订住客关联。',
    StayGuest: '在店住客关联。',
    InventoryHold: '库存占用：预订期间的房量占用事实。',
    CancellationPolicy: '取消政策：按时间窗的取消费规则。',
    DepositPolicy: '押金政策。',
    BookingAccessToken: '预订访问令牌：客人免登录查看/取消预订。',
    EarlyDeparturePolicy: '早离政策：提前离店的退费规则。',
  },
  'apps/billing/dec02.py': {},
};
