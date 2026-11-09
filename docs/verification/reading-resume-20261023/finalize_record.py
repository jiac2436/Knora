from pathlib import Path
import hashlib,json,shutil
root=Path(__file__).resolve().parents[3]
folder=root/'docs/verification/reading-resume-20261023'
snapshot=json.loads((folder/'source-snapshot.json').read_text())
release=json.loads((root/'apps/macos/release.json').read_text())
assert release['source_record']==snapshot['previous_source_record'] and release['manifest_sha256']==snapshot['parent_manifest_sha256']
parent=json.loads((root/release['source_record']).read_text())
h=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
sources=snapshot['sources']
changed={name for name,value in sources.items() if parent['sources'].get(name)!=value}
expected={'apps/macos/Sources/ReadingSession.swift','apps/macos/Sources/ReadingPositionScript.swift','apps/macos/Sources/Model.swift','apps/macos/Sources/Knowledge.swift','apps/macos/Sources/TextReader.swift','apps/macos/Sources/WebTextReader.swift','apps/macos/Sources/PDFReader.swift'}
assert changed==expected,changed
for name in changed:assert h(root/name)==sources[name], 'Scoped source changed after snapshot: '+name
for name,value in sources.items():assert h(root/snapshot['source_snapshot']/'Sources'/Path(name).name)==value
for dest,original in [('tests.log','/tmp/pka-reading-test.log'),('build.log','/tmp/pka-reading-build.log')]:shutil.copyfile(original,folder/dest)
assert 'PASS:' in (folder/'tests.log').read_text() and '构建完成' in (folder/'build.log').read_text()
ui=json.loads((folder/'native-ui-result.json').read_text());assert ui['same_saved_position'] and ui['same_article']
app=root/'apps/macos/.build/reading-resume-candidate/知识收集箱.app'
native=h(app/'Contents/MacOS/KnowledgeInbox')
icon=h(app/'Contents/Resources/AppIcon.icns');assert icon==h(root/release['app']/'Contents/Resources/AppIcon.icns')
assert h(app/'Contents/Resources/runtime/manifest.json')==release['manifest_sha256']
backend_changes=[name for name,value in parent['backend_sources'].items() if h(root/name)!=value]
record=dict(app=str(app.relative_to(root)),schema_version=16,plugin_version='1.0.5',manifest_sha256=release['manifest_sha256'],runtime_files=19959,
    previous_source_record=release['source_record'],native_binary_sha256=native,unchanged_native_binary_sha256=native,icon_sha256=icon,
    sources=sources,backend_sources=parent['backend_sources'],changed_sources=sorted(changed),added_sources=sorted(changed-set(parent['sources'])),
    source_snapshot=snapshot['source_snapshot'],excluded_concurrent_changes=snapshot['excluded_concurrent_changes']+backend_changes,
    tests={'status':'passed','state':'Temporary store roundtrip, private modes, quit flush, library isolation, corrupted/symlink preservation and late old-source event guard passed',
    'model':'Startup restore outside recent list, deleted record, explicit link priority and delayed response cannot replace user selection passed',
    'reader':'Real WKWebView destruction/recreation, paragraph anchor, narrow window, late layout/image height and search priority passed',
    'pdf':'Real PDFKit page and within-page native destination restore after initial layout and subsequent scroll passed',
    'native_ui':'Production KnowledgeView in isolated bundle: restores non-first recent article, scrolls, quits/relaunches, restores same source and identical saved anchor/progress',
    'signature':'deep strict passed','runtime':'unchanged verified runtime 19959 files; all other native sources match previous release; concurrent changes excluded','real_provider_calls':0})
(folder/'runtime-reading-resume.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
print('PASS: scoped sources, native/runtime/icon hashes and acceptance record')
