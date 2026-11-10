// Semantic authoring pass 3: hotel, hotelapp, tests, configs/docs/infra, scripts, templates
export const FILES = {
  'hotel/__init__.py': ['项目包标记文件（酒店 PMS 工程入口包）。', ['package', 'entry-point']],
  'hotel/settings.py': [
    'Django 项目配置：数据库、缓存、会话/Cookie、PMS 业务开关、日志与安全项设置。',
    ['configuration', 'django', 'settings', 'entry-point'],
  ],
  'hotel/urls.py': [
    '根 URL 路由：挂载 admin、前后台页面、API 与平台入口，是请求分发的总入口。',
    ['routing', 'urls', 'entry-point'],
  ],
  'hotel/wsgi.py': ['WSGI 部署入口，暴露 application 供生产服务器加载。', ['entry-point', 'deployment', 'wsgi']],

  'hotelapp/__init__.py': ['hotelapp 包标记文件。', ['package', 'barrel']],
  'hotelapp/admin.py': [
    '旧版酒店后台管理：客房、客户、订单、财务、夜审与账单等 Admin，含禁止删除混入。',
    ['admin', 'backoffice', 'legacy'],
  ],
  'hotelapp/apps.py': ['Django AppConfig，注册 hotelapp 应用。', ['django', 'app-config']],
  'hotelapp/audit_middleware.py': ['审计中间件：记录请求级审计日志。', ['middleware', 'audit']],
  'hotelapp/events.py': ['领域事件发布/订阅的轻量实现，供旧版流程解耦。', ['event', 'utility']],
  'hotelapp/handlers.py': ['事件处理器：订阅并处理发布的事件（审计、联动等）。', ['event-handler', 'utility']],
  'hotelapp/management/commands/audit_employee_data.py': [
    '管理命令：审计员工数据完整性与敏感字段。',
    ['management-command', 'audit', 'data-quality'],
  ],
  'hotelapp/management/commands/classify_users.py': [
    '管理命令：按权限/角色特征对存量用户分类，辅助迁移。',
    ['management-command', 'migration', 'identity'],
  ],
  'hotelapp/management/commands/issue_booking_tokens.py': [
    '管理命令：为存量预订补发预订访问令牌。',
    ['management-command', 'booking', 'migration'],
  ],
  'hotelapp/management/commands/reconcile_test_users.py': [
    '管理命令：核对并修正测试账号数据。',
    ['management-command', 'data-quality', 'test-support'],
  ],
  'hotelapp/management/commands/release_expired_reservations.py': [
    '管理命令：释放过期预订占用的库存。',
    ['management-command', 'reservation', 'scheduler'],
  ],
  'hotelapp/management/commands/seed_hotel_data.py': [
    '管理命令：初始化演示/基线数据（环境守卫，禁止生产误跑）。',
    ['management-command', 'seed', 'test-support'],
  ],
  'hotelapp/management/commands/seed_role_test_accounts.py': [
    '管理命令：按九类角色生成测试账号。',
    ['management-command', 'seed', 'test-support'],
  ],
  'hotelapp/models.py': [
    '旧版单体业务模型：客房、客户、餐饮、订单、停车、财务记录、夜审、账单、房间、员工与会员。',
    ['data-model', 'legacy', 'core-domain'],
  ],
  'hotelapp/state_machine.py': [
    '状态机约束：校验订单与房间状态流转是否合法。',
    ['utility', 'state-machine', 'domain-rule'],
  ],
  'hotelapp/templatetags/__init__.py': ['hotelapp 模板标签包标记。', ['package']],
  'hotelapp/templatetags/pms_filters.py': ['PMS 模板过滤器：金额、状态等展示格式化。', ['utility', 'template-tag']],
  'hotelapp/tests.py': [
    '旧版核心回归测试（超大）：权限矩阵、敏感字段脱敏、夜审幂等、员工生命周期与数据种子命令。',
    ['test', 'legacy', 'security'],
  ],
  'hotelapp/views.py': [
    '旧版页面视图（超大）：主界面、入住/离店、预订、财务、员工与导出等全部业务页与动作。',
    ['view', 'legacy', 'core-domain'],
  ],

  'api_tests/README.md': ['API 测试说明：如何运行 P0 冒烟测试与报告产出约定。', ['documentation', 'testing']],
  'api_tests/conftest.py': [
    'pytest 夹具：HTTP API 客户端、员工登录会话与测试数据准备。',
    ['test-fixture', 'pytest', 'utility'],
  ],
  'api_tests/pytest.ini': ['pytest 配置：测试路径、标记与输出选项。', ['configuration', 'testing']],
  'api_tests/reports/junit-p0.xml': ['P0 冒烟测试的 JUnit 报告产物（运行生成，非源码）。', ['test-report', 'generated']],
  'api_tests/test_p0_smoke.py': [
    'P0 冒烟测试：公开房量/预订、令牌查询与取消、未登录访问拦截、旧路由退役校验。',
    ['test', 'smoke', 'e2e'],
  ],
  'scripts/e2e_full_chain.py': [
    '端到端链路脚本：重置基线数据后走完预订-入住-收款-夜审-离店全流程并断言。',
    ['script', 'e2e', 'tooling'],
  ],
  'manage.py': ['Django 管理入口脚本。', ['entry-point', 'django', 'cli']],
  'apps/__init__.py': ['apps 包标记文件。', ['package', 'barrel']],
  '.github/workflows/ci.yml': [
    'GitHub Actions CI：安装依赖、静态检查与测试执行。',
    ['ci-cd', 'deployment', 'automation'],
  ],
  '.github/workflows/django-ci.yml': [
    'GitHub Actions Django 测试工作流：跑 Django 测试套件。',
    ['ci-cd', 'testing', 'django'],
  ],
  '.github/workflows/openapi-snapshot.yml': [
    'GitHub Actions OpenAPI 快照工作流：导出并比对 OpenAPI 契约快照。',
    ['ci-cd', 'openapi', 'contract'],
  ],
  '.github/pull_request_template.md': ['PR 模板：变更说明、验证方式与检查清单。', ['documentation', 'process']],
  '.env.example': [
    '环境变量样例：数据库、密钥与业务开关的必填项说明（不含真实密钥）。',
    ['configuration', 'environment', 'security'],
  ],
  'deploy_notes.md': ['部署说明：环境准备、初始化步骤与上线注意事项。', ['documentation', 'deployment']],
  'readme.md': ['项目说明：PMS 功能范围、技术栈、本地启动与目录结构。', ['documentation', 'entry-point', 'overview']],
  'requirements.txt': ['Python 依赖清单：Django 4.1 及相关运行库版本约束。', ['configuration', 'dependencies', 'python']],

  'templates/403.html': ['403 无权限页：越权访问时的提示页。', ['template', 'error-page']],
  'templates/_sidebar.html': ['公共侧边栏片段：按角色/模块权限渲染导航入口，被各页面 include。', ['template', 'navigation', 'shared-partial']],
  'templates/accomodation.html': ['客房管理页（旧版）：客房列表与增删改操作界面。', ['template', 'legacy', 'room']],
  'templates/booking.html': ['客房预订页：选择日期/房型、填写客人信息并提交预订。', ['template', 'booking']],
  'templates/checkin.html': ['入住管理页：待确认/已确认/在店列表与入住办理操作。', ['template', 'front-desk']],
  'templates/finance.html': ['财务管理页（旧版）：财务记录与收入图表。', ['template', 'legacy', 'finance']],
  'templates/force_password_change.html': ['强制改密页：首次登录/重置后必须修改密码。', ['template', 'security', 'auth']],
  'templates/frontdesk.html': ['前台接待工作台：到店、离店、在店与房间状态操作界面。', ['template', 'front-desk']],
  'templates/home.html': ['旧版主界面：经营概览、图表与快捷入口。', ['template', 'legacy', 'dashboard']],
  'templates/html1.html': ['旧版客房/订单列表页模板。', ['template', 'legacy']],
  'templates/html4.html': ['旧版酒店信息管理页模板。', ['template', 'legacy']],
  'templates/index.html': ['旧版首页模板：菜单与统计卡片。', ['template', 'legacy', 'dashboard']],
  'templates/login.html': ['员工登录页。', ['template', 'auth', 'login']],
  'templates/nightaudit.html': ['夜审页面（旧版）：夜审执行与营业日查看。', ['template', 'legacy', 'night-audit']],
  'templates/order.html': ['订单管理页（旧版）：订单列表与状态操作。', ['template', 'legacy', 'order']],
  'templates/park.html': ['停车管理页（旧版）。', ['template', 'legacy']],
  'templates/room.html': ['房间管理页（旧版）：房号与房态维护。', ['template', 'legacy', 'room']],
  'templates/staff_accounts.html': ['员工与账号管理页（只读存档，已由员工中心取代）。', ['template', 'legacy', 'staff']],
  'templates/staff_accounts_detail.html': ['员工详情页（只读存档）。', ['template', 'legacy', 'staff']],
  'templates/vip.html': ['会员管理页（旧版）：会员列表与等级。', ['template', 'legacy', 'vip']],
  'templates/platform/base.html': ['平台管理基础模板：平台侧布局与内容 block。', ['template', 'platform', 'layout']],
  'templates/platform/home.html': ['平台管理首页：系统概览。', ['template', 'platform']],
  'templates/platform/permissions.html': ['平台权限页：角色/权限矩阵维护界面。', ['template', 'platform', 'permission']],
  'templates/platform/system.html': ['平台系统信息页：运行环境与账号安全信息。', ['template', 'platform']],
  'templates/pms/audit_center.html': ['审计中心页面：审计事件检索、详情与汇总视图（含大量前端脚本）。', ['template', 'pms2', 'audit']],
  'templates/pms/billing_center.html': [
    '账务中心页面（超大）：账单、收款、退款、班次与审批操作的单页界面。',
    ['template', 'pms2', 'billing', 'finance'],
  ],
  'templates/pms/housekeeping_center.html': ['客房中心页面：房态看板、房态流转与维修单操作。', ['template', 'pms2', 'housekeeping']],
  'templates/pms/management_center.html': ['管理中心页面：员工、会员与组织管理的统一入口页。', ['template', 'pms2', 'management']],
  'templates/pms/me.html': ['个人中心页：当前员工信息与改密入口。', ['template', 'pms2', 'profile']],
  'templates/pms/night_audit_center.html': [
    '夜审中心页面（超大）：夜审执行、营业日、重开与破玻璃审批界面。',
    ['template', 'pms2', 'night-audit'],
  ],
  'templates/pms/notification_center.html': ['通知中心页面：待办/通知列表与已读操作。', ['template', 'pms2', 'notification']],
  'templates/pms/report_center.html': ['报表中心页面：出租率、ADR、收入等报表展示。', ['template', 'pms2', 'reporting']],
  'templates/pms/staff_center.html': ['员工中心页面（超大）：员工、成员关系、角色与邀请管理界面。', ['template', 'pms2', 'staff']],
  'templates/identity/invite_accept.html': ['员工邀请激活页：设置密码并激活账号。', ['template', 'identity', 'invitation']],
  'templates/public/booking.html': ['公开预订页（超大数据）：面向客人的房量查询与在线预订界面。', ['template', 'public', 'booking']],
};

export const CLASSES = {
  'hotelapp/models.py': {
    Accommodation: '住宿单（房单）：住客在房间的住宿事实与状态。',
    Client: '客户档案：姓名、证件与联系方式。',
    Food: '餐饮消费项。',
    Order: '订单：住宿/餐饮等订单主实体。',
    Park: '停车记录。',
    FinanceRecord: '财务记录：收支流水。',
    NightAudit: '夜审记录：营业日与结账标记。',
    Folio: '账单明细行（旧版）。',
    Room: '房间（旧版）：房号与房态。',
    Staff: '员工（旧版）。',
    Vip: '会员（旧版）：等级与折扣。',
  },
  'hotelapp/views.py': {},
};
