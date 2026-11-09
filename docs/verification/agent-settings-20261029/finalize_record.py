from pathlib import Path
import hashlib,json,subprocess,sys
ROOT=Path(__file__).resolve().parents[3];HERE=Path(__file__).parent
sys.path.insert(0,str(ROOT/'apps/macos/scripts/runtime'))
from verify_runtime import digest,verify

r=json.loads((HERE/'candidate-inputs.json').read_text())
app=ROOT/'apps/macos/.build/agent-settings-candidate/知识收集箱.app'
runtime=app/'Contents/Resources/runtime'
assert verify(runtime,r['manifest_sha256'])==3022
subprocess.run(['codesign','--verify','--deep','--strict',str(app)],check=True)
for key in ('sources','backend_sources'):
 for path,wanted in r[key].items():
  assert digest(ROOT/path)==wanted,path
assert digest(ROOT/'apps/macos/runtime/capability-roots.json')==r['capability_roots_sha256']
for file,phrase in [('backend-tests.log','1489 passed'),('agent-targeted-tests.log','64 passed'),
 ('agent-runtime.log','0 provider calls'),('native-checks.log','PASS:'),('base-cloud-mcp.log','saved result survive restart')]:
 assert phrase in (HERE/file).read_text(),file
for name in ['ui-overview.png','ui-search.png','ui-saved.txt','ui-claude.txt','ui-scroll.txt']:
 assert (HERE/name).is_file(),name
previous=ROOT/r['previous_release']['app']
assert digest(app/'Contents/Resources/AppIcon.icns')==digest(previous/'Contents/Resources/AppIcon.icns')
ocr=ROOT/'apps/macos/.build/ocr-candidate/知识收集箱.app/Contents/Resources/runtime'
for name in ['extension-catalog.json','dependency-manifests/ocr.json','dependency-manifests/ocr-compatibility.json']:
 assert digest(runtime/name)==digest(ocr/name),name
record={**r,'schema_version':16,'plugin_version':'1.0.5','app':str(app.relative_to(ROOT)),
 'unchanged_native_binary_sha256':digest(app/'Contents/MacOS/KnowledgeInbox'),
 'icon_sha256':digest(app/'Contents/Resources/AppIcon.icns'),
 'logical_app_bytes':sum(path.stat().st_size for path in app.rglob('*') if path.is_file() and not path.is_symlink()),
 'tests':{'status':'passed; candidate ready for publisher',
 'backend':'1489 passed, 1 skipped, 1 warning in complete suite; final 64 targeted tests also cover retained current clients beyond 100 history entries',
 'scope':'Real packaged API/MCP: same ID/token/expiry; immediate narrowed/expanded grants; future libraries excluded; independent Codex/Claude; old duplicate tokens rejected across restart',
 'plugin':'Real base-package plugin generation/MCP check/update preserves one package/config entry and unrelated settings; tomli restored from pinned accepted files; health uses exact search without optional semantic engine',
 'native':'Complete optimized build and strict signature; DTO/current-client/config regression; production Agent view with 36 synthetic libraries, bulk selection/search/scroll/save, single current card and independent client tabs verified in isolated home',
 'base':'Packaged base/MCP/cloud execution and restart use synthetic model replies; no real provider calls',
 'preserved':'Accepted pending OCR dependency installer/list/catalog/identities/native rules retained; schema16/plugin1.0.5/A icon preserved; browser_native.py excluded; no real user data, credentials or host configurations modified',
 'real_provider_calls':0}}
(HERE/'runtime-agent-settings.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
print('PASS: final candidate record, signature, sources, runtime, UI evidence and preserved OCR verified')
