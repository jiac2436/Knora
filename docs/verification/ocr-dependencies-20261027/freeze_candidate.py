"""Freeze OCR installer/list changes without incorporating concurrent work."""
from pathlib import Path
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).parent
sys.path.insert(0, str(ROOT / 'apps/macos/scripts/runtime'))
from verify_runtime import digest, verify
from build_runtime import tree_manifest

release = json.loads((ROOT / 'apps/macos/release.json').read_text())
record = json.loads((ROOT / release['source_record']).read_text())
runtime = ROOT / 'apps/macos/.build/ocr-runtime'
backend = dict(record['backend_sources'])
for name in ['extension_install.py', 'dependency_install.py', 'dependency_native.py']:
    shutil.copy2(ROOT / 'services/api/app' / name, runtime / 'backend/app' / name)
    backend['services/api/app/' + name] = digest(runtime / 'backend/app' / name)
snapshot = ROOT / 'apps/macos/.build/ocr-source'
if not snapshot.exists():
    shutil.copytree(ROOT / record['source_snapshot'], snapshot, ignore=shutil.ignore_patterns('.build'))
shutil.copy2(ROOT / 'apps/macos/Sources/LocalExtensions.swift', snapshot / 'Sources/LocalExtensions.swift')
sources = dict(record['sources'])
sources['apps/macos/Sources/LocalExtensions.swift'] = digest(snapshot / 'Sources/LocalExtensions.swift')
for name, wanted in sources.items():
    assert digest(snapshot / 'Sources' / Path(name).name) == wanted
manifest = runtime / 'manifest.json'
value = json.loads(manifest.read_text())
value['files'] = [item for item in tree_manifest(runtime) if item['path'] != 'manifest.json']
value['ocr_gcc_source_audit_sha256'] = digest(HERE / 'gcc-source.json')
manifest.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
result = dict(manifest_sha256=digest(manifest), runtime_files=verify(runtime, digest(manifest)),
    previous_release=release, source_snapshot=str(snapshot.relative_to(ROOT)), sources=sources, backend_sources=backend,
    excluded_concurrent_changes=['services/api/app/browser_native.py'])
(HERE / 'candidate-inputs.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print('PASS: frozen OCR runtime and native download-list sources;', result['runtime_files'], 'runtime entries')
