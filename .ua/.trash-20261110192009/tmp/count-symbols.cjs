const fs=require('fs');
const ua=process.argv[2];
const b=JSON.parse(fs.readFileSync(ua+'/intermediate/batches.json','utf8'));
let cls=0, fn=0, fn25=0, clsBig=0; const perFile=[];
const isTest=(p)=>/test_|_test|tests\.py$|conftest\.py$/.test(p);
for(const batch of b.batches){
  const r=JSON.parse(fs.readFileSync(ua+'/tmp/ua-file-extract-results-'+batch.batchIndex+'.json','utf8'));
  for(const f of r.results){
    if(isTest(f.path)) continue;
    const cc=(f.classes||[]).filter(c=>((c.methods||[]).length>=2)||((c.endLine-c.startLine)>=20));
    const ff=(f.functions||[]).filter(x=>(x.endLine-x.startLine)>=25);
    cls+=cc.length; fn+=ff.length;
    if(cc.length||ff.length) perFile.push(f.path+' :: C='+cc.map(c=>c.name).join(',')+' | F='+ff.map(x=>x.name).join(','));
  }
}
console.log('non-test classes(>=2m or >=20L):',cls,' functions(>=25L):',fn);
console.log(perFile.join('\n'));
