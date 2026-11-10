// Build a compact digest of every analyzed file for inline semantic authoring.
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const uaDir = process.argv[2];
const batches = JSON.parse(readFileSync(join(uaDir, 'intermediate', 'batches.json'), 'utf8'));

const docs = new Map();
const byBatch = new Map();
for (const b of batches.batches) {
  const res = JSON.parse(readFileSync(join(uaDir, 'tmp', `ua-file-extract-results-${b.batchIndex}.json`), 'utf8'));
  byBatch.set(b.batchIndex, { batch: b, results: res.results });
  for (const f of res.results) docs.set(f.path, f);
}

const out = [];
for (const [idx, { batch, results }] of byBatch) {
  out.push(`### BATCH ${idx} (${batch.files.length} files)`);
  for (const f of results) {
    const cls = (f.classes ?? []).map((c) => `${c.name}[${(c.methods ?? []).length}m,L${c.startLine}-${c.endLine}]`).join(' ');
    const fns = (f.functions ?? [])
      .filter((x) => x.endLine - x.startLine >= 10)
      .slice(0, 8)
      .map((x) => `${x.name}(${x.endLine - x.startLine}L)`)
      .join(' ');
    const doc = (f.docstring ?? '').replace(/\s+/g, ' ').slice(0, 90);
    const imps = (batch.batchImportData?.[f.path] ?? []).length;
    out.push(
      `- ${f.path} | ${f.fileCategory} | ${f.totalLines}L | imp=${imps}${doc ? ' | doc: ' + doc : ''}${cls ? ' | classes: ' + cls : ''}${fns ? ' | funcs: ' + fns : ''}`
    );
  }
}
writeFileSync(join(uaDir, 'tmp', 'digest.txt'), out.join('\n'));
console.log('digest lines:', out.length);
