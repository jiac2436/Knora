"""Freeze only browser image changes over the currently verified app."""
from pathlib import Path
import json, shutil, subprocess, sys
root = Path(__file__).resolve().parents[3]
sys.path.insert(0,str(root/'apps/macos/scripts/runtime'))
from verify_runtime import verify,digest
from build_runtime import tree_manifest
release=json.loads((root/'apps/macos/release.json').read_text())
baseline=json.loads((root/release['source_record']).read_text())
parent=root/release['app']/'Contents/Resources/runtime'
verify(parent,release['manifest_sha256'])
folder=Path(__file__).parent
runtime=root/'apps/macos/.build/runtime/browser-images-20261023'
assert not runtime.exists()
subprocess.run(['cp','-cR',str(parent),str(runtime)],check=True)
files={**{f'services/api/app/{name}.py':f'backend/app/{name}.py' for name in ('schemas','services','web_images','images','local_backup')},
       **{f'services/worker/{name}.py':f'backend/services/worker/{name}.py' for name in ('main','web_images')}}
for source,target in files.items(): shutil.copyfile(root/source,runtime/target)
manifest=runtime/'manifest.json'; data=json.loads(manifest.read_text());manifest.unlink()
data['parent_manifest_sha256']=release['manifest_sha256']; data['scope']='Browser image links: queued public download, reading, retries and backup; no dependency or database schema changes'
data['files']=tree_manifest(runtime);manifest.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
old={i['path']:i for i in json.loads((parent/'manifest.json').read_text())['files']}
changed=[i['path'] for i in data['files'] if i!=old.get(i['path'])]
assert set(changed)==set(files.values()), changed
checksum=digest(manifest); count=verify(runtime,checksum)
snapshot=root/'apps/macos/.build/browser-images-source'
assert not snapshot.exists()
shutil.copytree(root/baseline['source_snapshot'],snapshot)
source='apps/macos/Sources/LocalWebImages.swift'
assert digest(snapshot/'Sources/LocalWebImages.swift')==baseline['sources'][source]
shutil.copyfile(root/source,snapshot/'Sources/LocalWebImages.swift')
sources={name:digest(snapshot/'Sources'/Path(name).name) for name in baseline['sources']}
assert {name for name,value in sources.items() if value!=baseline['sources'][name]}=={source}
backend=dict(baseline['backend_sources']);backend.update({name:digest(root/name) for name in files})
record=dict(schema_version=16,plugin_version=release['plugin_version'],runtime=str(runtime.relative_to(root)),
 app='apps/macos/.build/browser-images-candidate/知识收集箱.app',source_snapshot=str(snapshot.relative_to(root)),sources=sources,backend_sources=backend,
 manifest_sha256=checksum,parent_manifest_sha256=release['manifest_sha256'],previous_source_record=release['source_record'],runtime_files=count,
 changed_runtime_files=changed,changed_sources=[source,*files],tests={'status':'verification_in_progress'})
(folder/'runtime-browser-images.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
print('PASS: frozen scoped runtime and native source; manifest',checksum)
