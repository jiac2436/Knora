from pathlib import Path
import json,shutil,sys
ROOT=Path(__file__).resolve().parents[3];HERE=Path(__file__).parent
sys.path.insert(0,str(ROOT/'apps/macos/scripts/runtime'))
from verify_runtime import verify,digest
from build_runtime import tree_manifest
release=json.loads((ROOT/'apps/macos/release.json').read_text());base=json.loads((ROOT/release['source_record']).read_text())
source=ROOT/release['app']/'Contents/Resources/runtime'
assert verify(source,release['manifest_sha256'])==release['runtime_files']
runtime=ROOT/'apps/macos/.build/plugin-singleton-runtime'
if not runtime.exists():shutil.copytree(source,runtime,symlinks=True)
backend=dict(base['backend_sources']);native=dict(base['sources'])
for name in ['agent_plugin.py']:
 shutil.copy2(ROOT/'services/api/app'/name,runtime/'backend/app'/name)
 backend['services/api/app/'+name]=digest(runtime/'backend/app'/name)
snapshot=ROOT/'apps/macos/.build/plugin-singleton-source'
if not snapshot.exists():shutil.copytree(ROOT/base['source_snapshot'],snapshot,ignore=shutil.ignore_patterns('.build'))
for name in ['LocalAgentInstaller.swift','LocalAgentConnections.swift']:
 shutil.copy2(ROOT/'apps/macos/Sources'/name,snapshot/'Sources'/name)
 native['apps/macos/Sources/'+name]=digest(snapshot/'Sources'/name)
for name,sha in native.items():assert digest(snapshot/'Sources'/Path(name).name)==sha
manifest=runtime/'manifest.json';value=json.loads(manifest.read_text());value['files']=[v for v in tree_manifest(runtime) if v['path']!='manifest.json'];manifest.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
r=dict(manifest_sha256=digest(manifest),runtime_files=verify(runtime,digest(manifest)),previous_release=release,source_snapshot=str(snapshot.relative_to(ROOT)),sources=native,backend_sources=backend,excluded_concurrent_changes=['services/api/app/browser_native.py'])
(HERE/'candidate-inputs.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
print('PASS: scoped runtime/native frozen;',r['runtime_files'],'entries')
