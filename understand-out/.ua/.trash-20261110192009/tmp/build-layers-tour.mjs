// Build architectural layers and guided tour from the merged node set (deterministic, path-based).
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const uaDir = process.argv[2];
const graph = JSON.parse(readFileSync(join(uaDir, 'intermediate', 'assembled-graph.json'), 'utf8'));
const scan = JSON.parse(readFileSync(join(uaDir, 'intermediate', 'scan-result.json'), 'utf8'));

const FILE_TYPES = new Set(['file', 'config', 'document', 'service', 'pipeline', 'table', 'schema', 'resource', 'endpoint']);
const fileNodes = graph.nodes.filter((n) => FILE_TYPES.has(n.type));
const byPath = new Map(fileNodes.map((n) => [n.filePath, n]));
const catOf = new Map(scan.files.map((f) => [f.path, f.fileCategory]));

const isTest = (p) =>
  /(^|\/)test_[^/]*\.py$/.test(p) ||
  /(^|\/)[^/]*_test\.py$/.test(p) ||
  /(^|\/)tests\.py$/.test(p) ||
  /(^|\/)conftest\.py$/.test(p) ||
  p.startsWith('api_tests/');

function layerFor(p) {
  if (p.startsWith('.github/workflows/')) return 'ci';
  if (catOf.get(p) === 'docs') return 'delivery';
  if (isTest(p)) return 'test';
  if (p.startsWith('templates/')) return 'frontend';
  if (p.startsWith('hotelapp/')) return 'legacy';
  if (p.startsWith('apps/api/')) return 'api';
  if (p.startsWith('apps/identity/')) return 'identity';
  if (p.startsWith('apps/billing/')) return 'billing';
  if (p.startsWith('apps/reservations/')) return 'reservations';
  if (p.startsWith('apps/inventory/')) return 'inventory';
  if (p.startsWith('apps/guests/')) return 'guests';
  if (p.startsWith('apps/core/')) return 'core';
  if (p.startsWith('scripts/')) return 'test';
  return 'entry'; // hotel/*, manage.py, .env.example, apps/__init__.py, 顶层文件
}

const SPEC = {
  entry: {
    name: '项目配置与入口',
    description: 'Django 工程配置、根路由、WSGI 与管理入口，以及环境变量样例等启动期基础设施。',
  },
  api: {
    name: 'HTTP API 与路由',
    description: '对外的 HTTP 接口层：路由注册、五大中心 API、公开预订与支付接口、OpenAPI 契约与页面渲染入口。',
  },
  identity: {
    name: '身份与权限',
    description: '员工档案与生命周期、物业成员关系、九角色权限注册表、会话/Cookie 隔离与邀请入职。',
  },
  billing: {
    name: '账务与财务',
    description: '账单/分录/收款/退款/退款负债/收银班次等不可变财务事实、批准编排、夜审与对账。',
  },
  reservations: {
    name: '预订与前台',
    description: '预订创建与取消、住店生命周期（排房/入住/续住/换房/离店）、散客入住与政策模型。',
  },
  inventory: {
    name: '库存与客房',
    description: '房型/房间/房价/日房量库存、房态流转、维修单与库存对账。',
  },
  guests: {
    name: '客人域',
    description: '客人主档、手机号/证件等 PII 的脱敏存储与按原因解密、会员会籍。',
  },
  core: {
    name: '核心基础能力',
    description: '物业/审计事件/营业日/破玻璃/幂等/限流/通知等共享模型与工具，含 API 契约工具库。',
  },
  legacy: {
    name: '旧版单体业务',
    description: 'hotelapp 中的旧版页面视图与模型（客房/订单/财务/夜审），逐步被 apps/* 现代化模块取代。',
  },
  frontend: {
    name: '前端模板',
    description: '服务端渲染的 Django 模板：PMS2 五大中心页、平台管理页、公开预订页与旧版页面。',
  },
  test: {
    name: '测试与端到端验证',
    description: 'Django 单元/回归测试、API 契约测试、端到端链路脚本与测试产物。',
  },
  delivery: {
    name: '文档与交付配置',
    description: 'README、部署说明、依赖清单与 PR 模板等交付期文档与配置。',
  },
  ci: {
    name: 'CI/CD 自动化',
    description: 'GitHub Actions 工作流：CI 检查、Django 测试与 OpenAPI 快照比对。',
  },
};

const members = new Map(Object.keys(SPEC).map((k) => [k, []]));
for (const n of fileNodes) {
  const key = layerFor(n.filePath);
  (members.get(key) ?? members.get('entry')).push(n.id);
}

const layers = Object.entries(SPEC)
  .map(([key, spec]) => ({
    id: `layer:${key}`,
    name: spec.name,
    description: spec.description,
    nodeIds: members.get(key) ?? [],
  }))
  .filter((l) => l.nodeIds.length > 0);

// ---- guided tour ----
const id = (p, type = 'file') => {
  const n = byPath.get(p);
  return n ? n.id : `${type}:${p}`;
};
const tour = [
  {
    order: 1,
    title: '项目总览',
    description:
      '先读 README 了解这套 Django 酒店 PMS（PMS2）的范围：多物业、九角色权限、预订-住店-账务-夜审核心流程。',
    nodeIds: [id('readme.md', 'document')],
  },
  {
    order: 2,
    title: '启动与配置',
    description: '从 manage.py 进入，经 hotel/settings.py 组装应用、中间件与会话策略，由 hotel/urls.py 分发请求。',
    nodeIds: [id('manage.py'), id('hotel/settings.py'), id('hotel/urls.py'), id('hotel/wsgi.py')],
  },
  {
    order: 3,
    title: '路由与接口层',
    description: 'apps/api/urls.py 注册全部 HTTP 端点：五大中心、公开预订、页面与平台入口。',
    nodeIds: [id('apps/api/urls.py'), id('apps/api/views.py'), id('apps/api/pages.py')],
  },
  {
    order: 4,
    title: '身份、权限与会话',
    description:
      '权限体系的骨架：注册表定义九角色与模块权限码，授权服务做判定，拆分会话中间件隔离 PMS 与平台入口。',
    nodeIds: [
      id('apps/identity/registry.py'),
      id('apps/identity/authorization.py'),
      id('apps/identity/session_middleware.py'),
      id('apps/identity/models.py'),
    ],
  },
  {
    order: 5,
    title: '预订到住店的主流程',
    description: '预订服务锁库存并落预订事实，前台服务负责排房/入住/收款/离店，住店生命周期服务处理续住与换房。',
    nodeIds: [
      id('apps/reservations/services.py'),
      id('apps/reservations/front_desk_services.py'),
      id('apps/reservations/lifecycle_services.py'),
      id('apps/reservations/models.py'),
    ],
  },
  {
    order: 6,
    title: '库存与客房联动',
    description: '房量库存与房态是可用性的两个来源，客房/维修状态变更会同步库存可用性，并由对账任务兜底。',
    nodeIds: [
      id('apps/inventory/services.py'),
      id('apps/inventory/models.py'),
      id('apps/inventory/reconciliation.py'),
    ],
  },
  {
    order: 7,
    title: '账务、审批与夜审',
    description:
      '账务以“不可变财务事实”为核心：收款/退款/冲销/调账都留事实与审批，夜审推进营业日并结转收入。',
    nodeIds: [
      id('apps/billing/models.py'),
      id('apps/billing/services_v2.py'),
      id('apps/billing/approval_registry.py'),
      id('apps/billing/night_audit.py'),
      id('apps/core/models.py'),
    ],
  },
  {
    order: 8,
    title: '前端中心页与公共预订页',
    description: 'PMS2 的五大中心以服务端渲染模板承载，共享侧边栏；公开预订页面向客人无需登录。',
    nodeIds: [
      id('templates/pms/billing_center.html'),
      id('templates/_sidebar.html'),
      id('templates/public/booking.html'),
      id('apps/api/pages.py'),
    ],
  },
  {
    order: 9,
    title: '质量保障与交付',
    description: '契约测试、白盒/回归测试与端到端脚本守住业务规则；GitHub Actions 跑测试并比对 OpenAPI 快照。',
    nodeIds: [
      id('apps/api/test_openapi_schema.py'),
      id('api_tests/test_p0_smoke.py'),
      id('scripts/e2e_full_chain.py'),
      id('.github/workflows/django-ci.yml', 'pipeline'),
    ],
  },
];

writeFileSync(join(uaDir, 'intermediate', 'layers.json'), JSON.stringify(layers, null, 2));
writeFileSync(join(uaDir, 'intermediate', 'tour.json'), JSON.stringify(tour, null, 2));
console.log('layers:', layers.map((l) => `${l.id}(${l.nodeIds.length})`).join(' '));
console.log('fileNodes:', fileNodes.length, 'assigned:', layers.reduce((a, l) => a + l.nodeIds.length, 0));
console.log('tour steps:', tour.length);
