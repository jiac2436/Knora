"""Assemble the scoped direct-model runtime on the verified formal release.

Creates an unverified candidate record; native build and acceptance precede publication.
"""
from pathlib import Path
import json, shutil, subprocess, sys
root=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(root/'apps/macos/scripts/runtime'))
from verify_runtime import verify,digest
from build_runtime import tree_manifest
release=json.loads((root/'apps/macos/release.json').read_text())
parent=root/release['app']/'Contents/Resources/runtime'
verify(parent,release['manifest_sha256'])
allowed={'services/api/app/embedded_gateway.py','services/worker/ai_analysis.py',
         'services/worker/domain_summary.py','services/worker/rag_answer.py'}
for directory,target in [('services/api/app','backend/app'),('services/worker','backend/services/worker')]:
 for path in (root/directory).rglob('*.py'):
  if '__pycache__' in path.parts: continue
  old=parent/target/path.relative_to(root/directory)
  relative=str(path.relative_to(root))
  if relative not in allowed:
   assert old.exists() and digest(old)==digest(path), 'Unrelated backend changes: '+relative
reference=json.loads((root/'docs/verification/a1/runtime-joint-images-20261017.json').read_text())
native_allowed={'apps/macos/Sources/LocalAI.swift','apps/macos/Sources/LocalModelConfiguration.swift','apps/macos/Sources/Workspace.swift'}
for path in (root/'apps/macos/Sources').glob('*.swift'):
 relative=str(path.relative_to(root))
 assert relative in native_allowed or reference['sources'].get(relative)==digest(path), 'Unrelated native changes: '+relative
runtime=root/'apps/macos/.build/runtime/rebuilt-20261019-v16-model-config-verified'
assert not runtime.exists(), 'Candidate runtime already exists'
subprocess.run(['cp','-cR',str(parent),str(runtime)],check=True)
for relative in sorted(allowed):
 source=root/relative
 target=runtime/('backend/app' if '/api/app/' in relative else 'backend/services/worker')/source.name
 shutil.copyfile(source,target)
for source in (root/'services/ai_gateway').rglob('*.py'):
 if '__pycache__' in source.parts: continue
 target=runtime/'backend/services/ai_gateway'/source.relative_to(root/'services/ai_gateway')
 target.parent.mkdir(parents=True,exist_ok=True)
 shutil.copyfile(source,target)
manifest=runtime/'manifest.json'
data=json.loads(manifest.read_text())
manifest.unlink()
data['parent_manifest_sha256']=release['manifest_sha256']
data['scope']='Desktop direct model configuration with in-process gateway; retain latest WeChat dual-stack reader'
data['files']=tree_manifest(runtime)
manifest.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
checksum=digest(manifest)
count=verify(runtime,checksum)
parent_entries={entry['path']: entry for entry in json.loads((parent/'manifest.json').read_text())['files']}
record=dict(schema_version=release['schema_version'],plugin_version=release['plugin_version'],
 runtime=str(runtime.relative_to(root)),app='apps/macos/.build/model-config-candidate/知识收集箱.app',
 manifest_sha256=checksum,runtime_files=count,parent_manifest_sha256=release['manifest_sha256'],
 sources={str(p.relative_to(root)):digest(p) for p in sorted((root/'apps/macos/Sources').glob('*.swift'))},
 changed_runtime_files={entry['path']:entry.get('sha256') for entry in data['files']
  if parent_entries.get(entry['path']) != entry},
 tests={'status':'verification_in_progress','real_model_calls':0,'user_content_edited':False})
(root/'docs/verification/a1/runtime-model-config-20261019.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'runtime':str(runtime),'manifest_sha256':checksum,'files':count}))
