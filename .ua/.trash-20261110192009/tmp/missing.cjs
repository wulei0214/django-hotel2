const fs=require('fs');
const ua=process.argv[2]; const root=process.argv[3];
const b=JSON.parse(fs.readFileSync(ua+'/intermediate/batches.json','utf8'));
const got=new Set();
for(const batch of b.batches){ const r=JSON.parse(fs.readFileSync(ua+'/tmp/ua-file-extract-results-'+batch.batchIndex+'.json','utf8')); for(const f of r.results) got.add(f.path); }
const all=b.batches.flatMap(x=>x.files.map(f=>f.path));
const missing=all.filter(p=>!got.has(p));
console.log('missing',missing.length);
const out=[];
for(const p of missing){
  const txt=fs.readFileSync(root+'/'+p,'utf8');
  const title=(txt.match(/<title>([^<]*)<\/title>/i)||[])[1]||'';
  const ext=(txt.match(/{%\s*extends\s+["']([^"']+)/)||[])[1]||'';
  const inc=[...txt.matchAll(/{%\s*include\s+["']([^"']+)/g)].map(m=>m[1]).slice(0,6);
  const blocks=[...txt.matchAll(/{%\s*block\s+(\w+)/g)].map(m=>m[1]).slice(0,8);
  const lines=txt.split('\n').length;
  out.push(`${p} | ${lines}L | title=${title.slice(0,40)} | extends=${ext} | include=${inc.join(',')} | blocks=${blocks.join(',')}`);
}
fs.writeFileSync(ua+'/tmp/missing.txt', out.join('\n'));
console.log(out.join('\n'));
