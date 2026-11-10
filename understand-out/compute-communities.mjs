import { readFileSync } from 'node:fs';
import Graph from 'graphology';
import louvain from 'graphology-communities-louvain';
const g0 = JSON.parse(readFileSync(process.argv[2] + '/knowledge-graph.json', 'utf8'));
const g = new Graph({ multi: true, type: 'directed' });
for (const n of g0.nodes) if (!g.hasNode(n.id)) g.addNode(n.id, { type: n.type });
let self = 0;
for (const e of g0.edges) {
  if (e.source === e.target) { self++; continue; }
  if (g.hasEdge(e.source, e.target)) continue;
  g.addEdge(e.source, e.target);
}
const und = new Graph({ type: 'undirected', multi: false });
g.forEachEdge((k, a, s, t) => { if (!und.hasEdge(s, t)) und.addEdge(s, t); });
const communities = louvain.detailed(und);
const counts = {};
for (const c of Object.values(communities.communities)) counts[c] = (counts[c] || 0) + 1;
const sizes = Object.values(counts).sort((a, b) => b - a);
const indeg = g0.nodes.map((n) => [n.id, g0.edges.filter((e) => e.target === n.id).length]).sort((a, b) => b[1] - a[1]);
console.log(JSON.stringify({ nodes: g0.nodes.length, edges: g0.edges.length, selfLoops: self, communities: sizes.length, modularity: communities.modularity?.toFixed?.(3) ?? communities.modularity, topCommunities: sizes.slice(0, 8), topInDegree: indeg.slice(0, 8) }, null, 2));
