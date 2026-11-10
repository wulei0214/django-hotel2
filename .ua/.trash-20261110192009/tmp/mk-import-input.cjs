const fs=require('fs');
const ua=process.argv[2];
const s=JSON.parse(fs.readFileSync(ua+'/intermediate/scan-result.json','utf8'));
const inp={projectRoot:process.argv[3],files:s.files.map(f=>({path:f.path,language:f.language,fileCategory:f.fileCategory}))};
fs.writeFileSync(ua+'/tmp/import-input.json',JSON.stringify(inp,null,2));
