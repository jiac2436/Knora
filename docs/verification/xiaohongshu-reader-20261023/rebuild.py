from pathlib import Path
import json,shutil,subprocess,os,hashlib
root=Path(__file__).resolve().parents[3];folder=Path(__file__).parent;p=folder/'runtime-xiaohongshu-reader.json';r=json.loads(p.read_text());release=json.loads((root/'apps/macos/release.json').read_text());assert release['source_record']==r['previous_source_record']
snapshot=root/r['source_snapshot']
for name in r['changed_sources']:
 shutil.copyfile(root/name,snapshot/'Sources'/Path(name).name)
 r['sources'][name]=hashlib.sha256((root/name).read_bytes()).hexdigest()
r['tests']['status']='verification_in_progress';p.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
env=os.environ.copy();env.update(PKA_APP_OUTPUT=str(root/r['app']),PKA_RUNTIME_PAYLOAD=str(root/release['app']/'Contents/Resources/runtime'),PKA_RUNTIME_MANIFEST_SHA256=r['manifest_sha256'])
with (folder/'build.log').open('w') as log:result=subprocess.run(['bash','scripts/build.sh'],cwd=snapshot,env=env,stdout=log,stderr=subprocess.STDOUT)
if result.returncode:print((folder/'build.log').read_text()[-2000:]);raise SystemExit(result.returncode)
subprocess.run(['codesign','--verify','--deep','--strict',str(root/r['app'])],check=True)
r['native_binary_sha256']=hashlib.sha256((root/r['app']/'Contents/MacOS/KnowledgeInbox').read_bytes()).hexdigest();p.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print('PASS: final source-matched build and signature')
