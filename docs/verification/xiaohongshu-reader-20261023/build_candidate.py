from pathlib import Path
import json,shutil,subprocess,os,sys
root=Path(__file__).resolve().parents[3];folder=Path(__file__).parent
sys.path.insert(0,str(root/'apps/macos/scripts/runtime'))
from verify_runtime import digest,verify
release=json.loads((root/'apps/macos/release.json').read_text());baseline=json.loads((root/release['source_record']).read_text())
allowed={'Knowledge.swift','TextReader.swift','WebTextReader.swift','ReaderSourceMap.swift','ReadingPositionScript.swift'}
snapshot=root/'apps/macos/.build/xiaohongshu-reader-source';assert not snapshot.exists()
snapshot.mkdir();shutil.copytree(root/baseline['source_snapshot']/'Sources',snapshot/'Sources')
for name,checksum in baseline['sources'].items():assert digest(snapshot/'Sources'/Path(name).name)==checksum,name
for name in allowed|{'XiaohongshuReader.swift'}:shutil.copyfile(root/'apps/macos/Sources'/name,snapshot/'Sources'/name)
for name in ('Resources','scripts'):shutil.copytree(root/baseline['source_snapshot']/name,snapshot/name)
sources={f'apps/macos/Sources/{p.name}':digest(p) for p in (snapshot/'Sources').glob('*.swift')}
changed={name for name,value in sources.items() if baseline['sources'].get(name)!=value}
assert changed=={'apps/macos/Sources/'+n for n in allowed|{'XiaohongshuReader.swift'}},changed
runtime=root/release['app']/'Contents/Resources/runtime';verify(runtime,release['manifest_sha256'])
r=dict(schema_version=release['schema_version'],plugin_version=release['plugin_version'],manifest_sha256=release['manifest_sha256'],runtime_files=release['runtime_files'],
 previous_source_record=release['source_record'],source_snapshot=str(snapshot.relative_to(root)),sources=sources,backend_sources=baseline['backend_sources'],
 changed_sources=sorted(changed),added_sources=['apps/macos/Sources/XiaohongshuReader.swift'],app='apps/macos/.build/xiaohongshu-reader-candidate/知识收集箱.app',tests={'status':'verification_in_progress'})
p=folder/'runtime-xiaohongshu-reader.json';p.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
env=os.environ.copy();env.update(PKA_APP_OUTPUT=str(root/r['app']),PKA_RUNTIME_PAYLOAD=str(runtime),PKA_RUNTIME_MANIFEST_SHA256=r['manifest_sha256'])
with (folder/'build.log').open('w') as log:result=subprocess.run(['bash','scripts/build.sh'],cwd=snapshot,env=env,stdout=log,stderr=subprocess.STDOUT)
if result.returncode:print((folder/'build.log').read_text()[-3000:]);raise SystemExit(result.returncode)
subprocess.run(['codesign','--verify','--deep','--strict',str(root/r['app'])],check=True)
r['native_binary_sha256']=digest(root/r['app']/'Contents/MacOS/KnowledgeInbox');r['icon_sha256']=digest(root/r['app']/'Contents/Resources/AppIcon.icns')
p.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print('PASS: scoped native build; backend and stored AI inputs unchanged')
