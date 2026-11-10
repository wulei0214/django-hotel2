const fs=require('fs');const ua=process.argv[2];
const b=JSON.parse(fs.readFileSync(ua+'/intermediate/batches.json','utf8'));
const nodes=[],edges=[];
for(const bat of b.batches){const j=JSON.parse(fs.readFileSync(ua+'/intermediate/batch-'+bat.batchIndex+'.json','utf8'));nodes.push(...j.nodes);edges.push(...j.edges);}
const withEdge=new Set();edges.forEach(e=>{withEdge.add(e.source);withEdge.add(e.target);});
const orph=nodes.filter(n=>!withEdge.has(n.id));
console.log('nodes',nodes.length,'edges',edges.length,'orphans',orph.length);
console.log(orph.map(n=>n.id).join('\n'));
