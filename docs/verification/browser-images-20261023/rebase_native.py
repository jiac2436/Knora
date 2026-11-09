"""Retain the newer published reading-resume native release, adding only image polling."""
from pathlib import Path
import json,shutil,subprocess,os,sys
root=Path(__file__).resolve().parents[3];folder=Path(__file__).parent
sys.path.insert(0,str(root/'apps/macos/scripts/runtime'))
from verify_runtime import digest,verify
release=json.loads((root/'apps/macos/release.json').read_text());baseline=json.loads((root/release['source_record']).read_text())
p=folder/'runtime-browser-images.json';r=json.loads(p.read_text())
assert release['manifest_sha256']==r['parent_manifest_sha256'],'New runtime needs separate review'
verify(root/release['app']/'Contents/Resources/runtime',release['manifest_sha256'])
snapshot=root/'apps/macos/.build/browser-images-reading-resume-source';assert not snapshot.exists()
snapshot.mkdir();shutil.copytree(root/baseline['source_snapshot']/'Sources',snapshot/'Sources')
for name in ('Resources','scripts'):shutil.copytree(root/baseline['source_snapshot']/name,snapshot/name)
for name,checksum in baseline['sources'].items():assert digest(snapshot/'Sources'/Path(name).name)==checksum,name
file=snapshot/'Sources/LocalWebImages.swift';text=file.read_text();old='var isWebImage: Bool { retrievalMethod == "wechat_ua" }';assert text.count(old)==1
file.write_text(text.replace(old,'var isWebImage: Bool { ["wechat_ua", "browser_public"].contains(retrievalMethod) }'))
r['previous_source_record']=release['source_record'];r['source_snapshot']=str(snapshot.relative_to(root))
r['sources']={name:digest(snapshot/'Sources'/Path(name).name) for name in baseline['sources']}
assert {n for n,v in r['sources'].items() if baseline['sources'].get(n)!=v}=={'apps/macos/Sources/LocalWebImages.swift'}
r['tests']['status']='verification_in_progress';r['rebased_over_reading_resume']=True
p.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
env=os.environ.copy();env.update(PKA_APP_OUTPUT=str(root/r['app']),PKA_RUNTIME_PAYLOAD=str(root/r['runtime']),PKA_RUNTIME_MANIFEST_SHA256=r['manifest_sha256'])
with (folder/'build.log').open('w') as log:result=subprocess.run(['bash','scripts/build.sh'],cwd=snapshot,env=env,stdout=log,stderr=subprocess.STDOUT)
if result.returncode:print((folder/'build.log').read_text()[-2500:]);raise SystemExit(result.returncode)
subprocess.run(['codesign','--verify','--deep','--strict',str(root/r['app'])],check=True)
r['native_binary_sha256']=digest(root/r['app']/'Contents/MacOS/KnowledgeInbox')
r['tests']['status']='passed';r['tests']['native']='Newer published reading-resume source snapshot retained; only LocalWebImages.swift changed. Complete optimized build and strict signature passed.'
p.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
print('PASS: candidate retains reading resume, with scoped image support')
