const fs=require('fs');
const ua=process.argv[2];
const s=JSON.parse(fs.readFileSync(ua+'/intermediate/scan-result.json','utf8'));
const im=JSON.parse(fs.readFileSync(ua+'/tmp/import-map.json','utf8'));
s.importMap=im.importMap;
fs.writeFileSync(ua+'/intermediate/scan-result.json',JSON.stringify(s,null,2));
console.log('merged importMap for',Object.keys(s.importMap).length,'files');
