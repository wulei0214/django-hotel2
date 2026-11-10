// Helper: build per-batch extract-structure input and run extraction for all batches.
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';

const root = process.argv[2];
const uaDir = process.argv[3];
const skillDir = process.argv[4];

const batches = JSON.parse(readFileSync(join(uaDir, 'intermediate', 'batches.json'), 'utf8'));
mkdirSync(join(uaDir, 'tmp'), { recursive: true });

const summary = [];
for (const batch of batches.batches) {
  const idx = batch.batchIndex;
  const inputPath = join(uaDir, 'tmp', `ua-file-analyzer-input-${idx}.json`);
  const outPath = join(uaDir, 'tmp', `ua-file-extract-results-${idx}.json`);
  const payload = {
    projectRoot: root,
    batchFiles: batch.files.map((f) => ({
      path: f.path,
      language: f.language,
      sizeLines: f.sizeLines,
      fileCategory: f.fileCategory,
    })),
    batchImportData: batch.batchImportData ?? {},
  };
  writeFileSync(inputPath, JSON.stringify(payload, null, 2));
  const r = spawnSync(process.execPath, [join(skillDir, 'extract-structure.mjs'), inputPath, outPath], {
    encoding: 'utf8',
  });
  const ok = existsSync(outPath);
  let files = 0;
  let funcs = 0;
  let classes = 0;
  if (ok) {
    const res = JSON.parse(readFileSync(outPath, 'utf8'));
    files = res.filesAnalyzed ?? 0;
    for (const f of res.results) {
      funcs += (f.functions ?? []).length;
      classes += (f.classes ?? []).length;
    }
  }
  summary.push({ batch: idx, files: batch.files.length, exit: r.status, outputExists: ok, analyzed: files, funcs, classes, stderr: (r.stderr || '').trim().slice(0, 400) });
}
writeFileSync(join(uaDir, 'tmp', 'extract-summary.json'), JSON.stringify(summary, null, 2));
console.log(JSON.stringify(summary, null, 2));
