// Assemble final KnowledgeGraph, run inline deterministic validation, save graph.
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { execSync } from 'node:child_process';

const uaDir = process.argv[2];
const projectRoot = process.argv[3];
const commit = execSync('git rev-parse HEAD', { cwd: projectRoot, encoding: 'utf8' }).trim();

const assembled = JSON.parse(readFileSync(join(uaDir, 'intermediate', 'assembled-graph.json'), 'utf8'));
const layers = JSON.parse(readFileSync(join(uaDir, 'intermediate', 'layers.json'), 'utf8'));
const tour = JSON.parse(readFileSync(join(uaDir, 'intermediate', 'tour.json'), 'utf8'));

const graph = {
  version: '1.0.0',
  project: {
    name: 'django-hotel · PMS2 酒店管理系统',
    languages: ['python', 'html', 'javascript', 'css', 'markdown', 'yaml', 'sql', 'json'],
    frameworks: ['Django 4.1'],
    description:
      '基于 Django 4.1 的酒店 PMS（PMS2）：多物业隔离、九角色权限体系，覆盖预订、住店、房态库存、账务财务与夜审的主流程，含五大业务中心页面与面向客人的公开预订页。',
    analyzedAt: new Date().toISOString(),
    gitCommitHash: commit,
  },
  nodes: assembled.nodes,
  edges: assembled.edges,
  layers,
  tour,
};

// ---- inline deterministic validation (Phase 6 default path) ----
const issues = [];
const warnings = [];
const nodeIds = new Set();
const seen = new Map();
graph.nodes.forEach((n, i) => {
  if (!n.id) return issues.push(`Node[${i}] missing id`);
  if (!n.type) issues.push(`Node '${n.id}' missing type`);
  if (!n.name) issues.push(`Node '${n.id}' missing name`);
  if (!n.summary) issues.push(`Node '${n.id}' missing summary`);
  if (!n.tags || !n.tags.length) issues.push(`Node '${n.id}' missing tags`);
  if (seen.has(n.id)) issues.push(`Duplicate node ID '${n.id}'`);
  else seen.set(n.id, i);
  nodeIds.add(n.id);
});
let selfEdges = 0;
graph.edges.forEach((e, i) => {
  if (!nodeIds.has(e.source)) issues.push(`Edge[${i}] source '${e.source}' not found`);
  if (!nodeIds.has(e.target)) issues.push(`Edge[${i}] target '${e.target}' not found`);
  if (e.source === e.target) selfEdges++;
});
const FILE_TYPES = new Set(['file', 'config', 'document', 'service', 'pipeline', 'table', 'schema', 'resource', 'endpoint']);
const fileNodes = graph.nodes.filter((n) => FILE_TYPES.has(n.type)).map((n) => n.id);
const assigned = new Map();
graph.layers.forEach((l) => {
  (l.nodeIds || []).forEach((id) => {
    if (!nodeIds.has(id)) issues.push(`Layer '${l.id}' refs missing node '${id}'`);
    if (assigned.has(id)) issues.push(`Node '${id}' appears in multiple layers`);
    assigned.set(id, l.id);
  });
});
fileNodes.forEach((id) => {
  if (!assigned.has(id)) issues.push(`File node '${id}' not in any layer`);
});
graph.tour.forEach((s, i) => {
  (s.nodeIds || []).forEach((id) => {
    if (!nodeIds.has(id)) issues.push(`Tour step[${i}] refs missing node '${id}'`);
  });
});
const withEdges = new Set([...graph.edges.map((e) => e.source), ...graph.edges.map((e) => e.target)]);
graph.nodes.forEach((n) => {
  if (!withEdges.has(n.id)) warnings.push(`Node '${n.id}' has no edges (orphan)`);
});

const stats = {
  totalNodes: graph.nodes.length,
  totalEdges: graph.edges.length,
  totalLayers: graph.layers.length,
  tourSteps: graph.tour.length,
  nodeTypes: graph.nodes.reduce((a, n) => ((a[n.type] = (a[n.type] || 0) + 1), a), {}),
  edgeTypes: graph.edges.reduce((a, e) => ((a[e.type] = (a[e.type] || 0) + 1), a), {}),
  selfEdges,
};

writeFileSync(join(uaDir, 'intermediate', 'assembled-graph.json'), JSON.stringify(graph, null, 2));
writeFileSync(join(uaDir, 'intermediate', 'review.json'), JSON.stringify({ issues, warnings, stats }, null, 2));
console.log(JSON.stringify({ issues: issues.length, warnings: warnings.length, stats }, null, 2));
if (issues.length) console.log('ISSUES:\n' + issues.slice(0, 15).join('\n'));
if (warnings.length) console.log('WARNINGS:\n' + warnings.slice(0, 10).join('\n'));
