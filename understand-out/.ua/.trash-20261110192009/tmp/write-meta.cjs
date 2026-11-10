const fs=require('fs');const ua=process.argv[2];const root=process.argv[3];
const s=JSON.parse(fs.readFileSync(ua+'/intermediate/scan-result.json','utf8'));
const commit=require('child_process').execSync('git rev-parse HEAD',{cwd:root,encoding:'utf8'}).trim();
fs.writeFileSync(ua+'/intermediate/fingerprint-input.json',JSON.stringify({projectRoot:root,filePaths:s.files.map(f=>f.path),gitCommitHash:commit},null,2));
fs.writeFileSync(ua+'/meta.json',JSON.stringify({lastAnalyzedAt:new Date().toISOString(),gitCommitHash:commit,version:'1.0.0',analyzedFiles:s.files.length},null,2));
console.log('files',s.files.length,'commit',commit);
