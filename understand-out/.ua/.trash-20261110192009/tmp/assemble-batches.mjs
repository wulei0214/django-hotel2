// Assemble per-batch graph fragments from scan + extraction + authored semantics.
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { FILES as F1, CLASSES as C1 } from './sem1.mjs';
import { FILES as F2, CLASSES as C2 } from './sem2.mjs';
import { FILES as F3, CLASSES as C3 } from './sem3.mjs';

const uaDir = process.argv[2];
const batches = JSON.parse(readFileSync(join(uaDir, 'intermediate', 'batches.json'), 'utf8'));
const scan = JSON.parse(readFileSync(join(uaDir, 'intermediate', 'scan-result.json'), 'utf8'));

const FILES = { ...F1, ...F2, ...F3 };
const CLASSES = { ...C1, ...C2, ...C3 };

const fileInfo = new Map(scan.files.map((f) => [f.path, f]));
const missingSem = scan.files.filter((f) => !FILES[f.path]).map((f) => f.path);
if (missingSem.length) {
  console.log('MISSING SEMANTICS (' + missingSem.length + '):');
  console.log(missingSem.join('\n'));
}

function nodeType(cat, path) {
  if (cat === 'code' || cat === 'script' || cat === 'markup') return 'file';
  if (cat === 'config') return 'config';
  if (cat === 'docs') return 'document';
  if (cat === 'infra') return /\.github\/workflows\//.test(path) ? 'pipeline' : 'service';
  if (cat === 'data') return 'table';
  return 'file';
}
function prefixFor(type) {
  return (
    {
      file: 'file',
      config: 'config',
      document: 'document',
      pipeline: 'pipeline',
      service: 'service',
      table: 'table',
      schema: 'schema',
      endpoint: 'endpoint',
      resource: 'resource',
    }[type] || 'file'
  );
}
function complexityFor(lines) {
  if (lines < 50) return 'simple';
  if (lines <= 200) return 'moderate';
  return 'complex';
}

const fileNodeIds = new Map(); // path -> id
const nodesByPath = new Map(); // path -> node
for (const f of scan.files) {
  const sem = FILES[f.path] || ['无摘要。', ['untagged']];
  const type = nodeType(f.fileCategory, f.path);
  const prefix = prefixFor(type);
  const isTable = type === 'table';
  const id = isTable
    ? `table:${f.path}:audit_append_only_guards`
    : `${prefix}:${f.path}`;
  fileNodeIds.set(f.path, id);
  nodesByPath.set(f.path, {
    id,
    type,
    name: isTable ? 'audit_append_only_guards' : f.path.split('/').pop(),
    filePath: f.path,
    summary: sem[0],
    tags: sem[1].length ? sem[1] : ['untagged'],
    complexity: complexityFor(f.sizeLines ?? 0),
  });
}

// class nodes
const classNodes = [];
const classEdges = [];
const classSummarySeen = new Set();
for (const [path, spec] of Object.entries(CLASSES)) {
  const owner = nodesByPath.get(path);
  if (!owner) {
    console.log('WARN class owner not in graph:', path);
    continue;
  }
  const sem = FILES[path] || ['', ['untagged']];
  for (const [name, summary] of Object.entries(spec)) {
    if (!summary) continue;
    classSummarySeen.add(path + '::' + name);
    const id = `class:${path}:${name}`;
    const node = {
      id,
      type: 'class',
      name,
      filePath: path,
      summary,
      tags: [sem[1][0] || 'domain', sem[1][1] || 'django', 'class'],
      complexity: 'moderate',
    };
    classNodes.push(node);
    classEdges.push({ source: owner.id, target: id, type: 'contains', direction: 'forward', weight: 1.0 });
    classEdges.push({ source: owner.id, target: id, type: 'exports', direction: 'forward', weight: 0.8 });
  }
}

// extra semantic edges (non-import)
const extraEdges = [
  ['pipeline:.github/workflows/django-ci.yml', 'file:manage.py', 'triggers', 0.6],
  ['pipeline:.github/workflows/ci.yml', 'document:requirements.txt', 'triggers', 0.6],
  ['pipeline:.github/workflows/openapi-snapshot.yml', 'file:apps/api/openapi_schema.py', 'triggers', 0.6],
  ['pipeline:.github/workflows/django-ci.yml', 'file:apps/api/urls.py', 'triggers', 0.6],
  ['config:.env.example', 'file:hotel/settings.py', 'configures', 0.6],
  ['config:api_tests/pytest.ini', 'file:api_tests/test_p0_smoke.py', 'configures', 0.6],
  ['document:readme.md', 'file:manage.py', 'documents', 0.5],
  ['document:deploy_notes.md', 'file:hotel/settings.py', 'documents', 0.5],
  ['document:api_tests/README.md', 'file:api_tests/test_p0_smoke.py', 'documents', 0.5],
  ['document:requirements.txt', 'file:manage.py', 'configures', 0.6],
  ['document:readme.md', 'file:hotel/urls.py', 'documents', 0.5],
  ['table:apps/core/sql/audit_immutability.sql:audit_append_only_guards', 'file:apps/core/models.py', 'migrates', 0.7],
  ['file:apps/api/urls.py', 'file:apps/api/views.py', 'depends_on', 0.6],
  ['file:hotel/urls.py', 'file:apps/api/urls.py', 'depends_on', 0.6],
  ['file:apps/api/pages.py', 'file:apps/identity/authorization.py', 'depends_on', 0.6],
  ['file:manage.py', 'file:hotel/settings.py', 'depends_on', 0.6],
];

// template -> rendering view (verified by grepping the source for each template path)
const TPL_VIEW = {
  'templates/accomodation.html': 'hotelapp/views.py',
  'templates/booking.html': 'apps/api/pages.py',
  'templates/checkin.html': 'hotelapp/views.py',
  'templates/finance.html': 'hotelapp/views.py',
  'templates/force_password_change.html': 'hotelapp/views.py',
  'templates/frontdesk.html': 'hotelapp/views.py',
  'templates/home.html': 'hotelapp/views.py',
  'templates/html1.html': 'hotelapp/views.py',
  'templates/html4.html': 'hotelapp/views.py',
  'templates/index.html': 'hotelapp/views.py',
  'templates/login.html': 'hotelapp/views.py',
  'templates/order.html': 'hotelapp/views.py',
  'templates/park.html': 'hotelapp/views.py',
  'templates/room.html': 'hotelapp/views.py',
  'templates/staff_accounts.html': 'hotelapp/views.py',
  'templates/staff_accounts_detail.html': 'hotelapp/views.py',
  'templates/vip.html': 'hotelapp/views.py',
  'templates/identity/invite_accept.html': 'apps/identity/invitation_views.py',
  'templates/platform/home.html': 'apps/identity/platform.py',
  'templates/platform/permissions.html': 'apps/identity/permission_admin.py',
  'templates/platform/system.html': 'apps/identity/platform.py',
  'templates/pms/audit_center.html': 'apps/api/pages.py',
  'templates/pms/billing_center.html': 'apps/api/pages.py',
  'templates/pms/housekeeping_center.html': 'apps/api/pages.py',
  'templates/pms/management_center.html': 'apps/api/pages.py',
  'templates/pms/me.html': 'hotelapp/views.py',
  'templates/pms/night_audit_center.html': 'apps/api/pages.py',
  'templates/pms/notification_center.html': 'apps/api/pages.py',
  'templates/pms/report_center.html': 'apps/api/pages.py',
  'templates/pms/staff_center.html': 'apps/api/pages.py',
  'templates/public/booking.html': 'apps/api/pages.py',
};
for (const [tpl, view] of Object.entries(TPL_VIEW)) {
  extraEdges.push([`file:${view}`, `file:${tpl}`, 'depends_on', 0.6]);
}
// templates that {% include "_sidebar.html" %}
for (const tpl of [
  'templates/accomodation.html', 'templates/checkin.html', 'templates/finance.html', 'templates/frontdesk.html',
  'templates/home.html', 'templates/html1.html', 'templates/html4.html', 'templates/index.html',
  'templates/nightaudit.html', 'templates/order.html', 'templates/park.html', 'templates/room.html',
  'templates/staff_accounts.html', 'templates/staff_accounts_detail.html', 'templates/vip.html',
  'templates/pms/audit_center.html', 'templates/pms/billing_center.html', 'templates/pms/housekeeping_center.html',
  'templates/pms/management_center.html', 'templates/pms/me.html', 'templates/pms/night_audit_center.html',
  'templates/pms/notification_center.html', 'templates/pms/report_center.html', 'templates/pms/staff_center.html',
]) {
  extraEdges.push([`file:${tpl}`, 'file:templates/_sidebar.html', 'depends_on', 0.6]);
}
for (const tpl of ['templates/platform/home.html', 'templates/platform/permissions.html', 'templates/platform/system.html']) {
  extraEdges.push([`file:${tpl}`, 'file:templates/platform/base.html', 'depends_on', 0.6]);
}
// remaining meaningful links for otherwise isolated nodes
extraEdges.push(
  ['file:api_tests/test_p0_smoke.py', 'file:api_tests/conftest.py', 'depends_on', 0.6],
  ['config:api_tests/reports/junit-p0.xml', 'file:api_tests/test_p0_smoke.py', 'related', 0.5],
  ['file:apps/api/urls.py', 'file:apps/api/test_finance_route_regressions.py', 'tested_by', 0.5],
  ['file:apps/api/pages.py', 'file:apps/api/test_center_template_regressions.py', 'tested_by', 0.5],
  ['file:apps/api/urls.py', 'file:apps/api/test_dup_convergence_guards.py', 'tested_by', 0.5],
  ['file:apps/core/models.py', 'file:apps/core/labels.py', 'related', 0.5],
  ['table:apps/core/sql/audit_immutability.sql:audit_append_only_guards', 'file:apps/core/management/commands/check_mysql_append_only_guards.py', 'related', 0.5],
  ['table:apps/core/sql/audit_immutability.sql:audit_append_only_guards', 'file:apps/core/management/commands/install_mysql_append_only_guards.py', 'related', 0.5],
  ['file:apps/identity/platform.py', 'file:apps/identity/management/commands/platform_superadmin_check.py', 'related', 0.5],
  ['file:hotel/settings.py', 'file:hotelapp/audit_middleware.py', 'depends_on', 0.6],
  ['file:hotel/wsgi.py', 'file:hotel/settings.py', 'depends_on', 0.6],
  ['file:scripts/e2e_full_chain.py', 'file:apps/api/urls.py', 'related', 0.5],
  ['file:hotelapp/views.py', 'file:hotelapp/templatetags/pms_filters.py', 'related', 0.5],
  ['file:manage.py', 'file:hotel/wsgi.py', 'depends_on', 0.6],
  ['file:hotel/settings.py', 'file:templates/403.html', 'related', 0.5],
  ['document:.github/pull_request_template.md', 'file:manage.py', 'related', 0.5],
  ['file:hotelapp/templatetags/__init__.py', 'file:hotelapp/templatetags/pms_filters.py', 'related', 0.5],
);
// package markers / AppConfigs
for (const [marker, target] of [
  ['apps/api/apps.py', 'apps/api/urls.py'],
  ['apps/billing/apps.py', 'apps/billing/models.py'],
  ['apps/core/apps.py', 'apps/core/models.py'],
  ['apps/guests/apps.py', 'apps/guests/models.py'],
  ['apps/identity/apps.py', 'apps/identity/models.py'],
  ['apps/inventory/apps.py', 'apps/inventory/models.py'],
  ['apps/reservations/apps.py', 'apps/reservations/models.py'],
  ['hotelapp/apps.py', 'hotelapp/models.py'],
  ['apps/__init__.py', 'manage.py'],
  ['apps/api/__init__.py', 'apps/api/apps.py'],
  ['apps/guests/__init__.py', 'apps/guests/apps.py'],
  ['apps/inventory/__init__.py', 'apps/inventory/apps.py'],
  ['apps/reservations/__init__.py', 'apps/reservations/apps.py'],
  ['apps/guests/templatetags/__init__.py', 'apps/guests/services.py'],
  ['hotel/__init__.py', 'hotel/settings.py'],
  ['apps/billing/management/__init__.py', 'apps/billing/management/commands/expire_pending_refunds.py'],
  ['apps/billing/management/commands/__init__.py', 'apps/billing/management/commands/expire_pending_refunds.py'],
  ['apps/core/management/__init__.py', 'apps/core/management/commands/install_mysql_append_only_guards.py'],
  ['apps/core/management/commands/__init__.py', 'apps/core/management/commands/install_mysql_append_only_guards.py'],
  ['apps/identity/management/__init__.py', 'apps/identity/management/commands/expire_invitations.py'],
  ['apps/identity/management/commands/__init__.py', 'apps/identity/management/commands/expire_invitations.py'],
  ['apps/inventory/management/__init__.py', 'apps/inventory/management/commands/sync_inventory_calendar.py'],
  ['apps/inventory/management/commands/__init__.py', 'apps/inventory/management/commands/sync_inventory_calendar.py'],
  ['apps/reservations/management/__init__.py', 'apps/reservations/management/commands/expire_reservation_holds.py'],
  ['apps/reservations/management/commands/__init__.py', 'apps/reservations/management/commands/expire_reservation_holds.py'],
]) {
  extraEdges.push([`file:${marker}`, `file:${target}`, 'depends_on', 0.6]);
}

const allExtraEdges = extraEdges.map(([source, target, type, weight]) => ({ source, target, type, direction: 'forward', weight }));

// distribute edges into batches by source file
const batchOfPath = new Map();
for (const b of batches.batches) for (const f of b.files) batchOfPath.set(f.path, b.batchIndex);
const pathOfNodeId = new Map();
for (const [p, id] of fileNodeIds) pathOfNodeId.set(id, p);
for (const n of classNodes) pathOfNodeId.set(n.id, n.filePath);

const batchNodes = new Map();
const batchEdges = new Map();
for (const b of batches.batches) {
  batchNodes.set(b.batchIndex, []);
  batchEdges.set(b.batchIndex, []);
}
const fallbackBatch = batches.batches[0].batchIndex;

for (const [path, node] of nodesByPath) {
  const idx = batchOfPath.get(path) ?? fallbackBatch;
  batchNodes.get(idx).push(node);
}
for (const n of classNodes) {
  const idx = batchOfPath.get(n.filePath) ?? fallbackBatch;
  batchNodes.get(idx).push(n);
}
for (const e of classEdges) {
  const p = pathOfNodeId.get(e.source);
  const idx = batchOfPath.get(p) ?? fallbackBatch;
  batchEdges.get(idx).push(e);
}
for (const e of allExtraEdges) {
  const p = pathOfNodeId.get(e.source);
  const idx = p !== undefined ? batchOfPath.get(p) ?? fallbackBatch : fallbackBatch;
  batchEdges.get(idx).push(e);
}

// import edges 1:1 from batchImportData
let importCount = 0;
const seenEdge = new Set();
for (const b of batches.batches) {
  const imp = b.batchImportData || {};
  for (const [src, targets] of Object.entries(imp)) {
    const srcId = fileNodeIds.get(src);
    if (!srcId) continue;
    for (const t of targets || []) {
      const tgtId = fileNodeIds.get(t);
      if (!tgtId || tgtId === srcId) continue;
      const key = srcId + '|' + tgtId + '|imports';
      if (seenEdge.has(key)) continue;
      seenEdge.add(key);
      batchEdges.get(b.batchIndex).push({ source: srcId, target: tgtId, type: 'imports', direction: 'forward', weight: 0.7 });
      importCount++;
    }
  }
}

let totalNodes = 0;
let totalEdges = 0;
for (const [idx, nodes] of batchNodes) {
  const edges = batchEdges.get(idx);
  // drop edges whose endpoints are unknown at merge time? merge script drops dangling; keep all
  writeFileSync(join(uaDir, 'intermediate', `batch-${idx}.json`), JSON.stringify({ nodes, edges }, null, 2));
  totalNodes += nodes.length;
  totalEdges += edges.length;
  console.log(`batch-${idx}: nodes=${nodes.length} edges=${edges.length}`);
}
console.log(`TOTAL nodes=${totalNodes} edges=${totalEdges} imports=${importCount} classNodes=${classNodes.length}`);

