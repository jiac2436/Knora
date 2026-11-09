"""Freeze only the requested native reading-state change, preserving concurrent work."""
from pathlib import Path
import hashlib,json,shutil
root=Path(__file__).resolve().parents[3]
release=json.loads((root/'apps/macos/release.json').read_text())
parent=json.loads((root/release['source_record']).read_text())
snapshot=root/'apps/macos/.build/reading-resume-source'
(snapshot/'Sources').mkdir(parents=True,exist_ok=True)
changed={'Knowledge.swift','Model.swift','PDFReader.swift','TextReader.swift','WebTextReader.swift'}
added={'ReadingSession.swift','ReadingPositionScript.swift'}
h=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
excluded=[]
for name,checksum in parent['sources'].items():
    source=root/name
    if source.name not in changed and h(source)!=checksum:
        excluded.append(name)
        source=root/'apps/macos/.build/library-update-source/Sources'/source.name
        assert h(source)==checksum, 'No verified baseline available for '+name
    shutil.copyfile(source,snapshot/'Sources'/source.name)
for name in added:shutil.copyfile(root/'apps/macos/Sources'/name,snapshot/'Sources'/name)
for directory in ('Resources','scripts'):
    shutil.copytree(root/'apps/macos'/directory,snapshot/directory,dirs_exist_ok=True)
record=dict(previous_source_record=release['source_record'],parent_manifest_sha256=release['manifest_sha256'],excluded_concurrent_changes=excluded,
    sources={'apps/macos/Sources/'+p.name:h(p) for p in sorted((snapshot/'Sources').glob('*.swift'))},
    source_snapshot=str(snapshot.relative_to(root)))
(root/'docs/verification/reading-resume-20261023/source-snapshot.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n')
print('PASS: frozen 5 changed and 2 added Swift files; excluded '+str(excluded))
