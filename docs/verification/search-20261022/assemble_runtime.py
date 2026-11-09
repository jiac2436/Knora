"""Assemble complete same-version backend trees on the verified runtime; no dependency changes."""
from pathlib import Path
import json
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root / 'apps/macos/scripts/runtime'))
from verify_runtime import verify, digest
from build_runtime import tree_manifest

release = json.loads((root / 'apps/macos/release.json').read_text())
parent = root / release['app'] / 'Contents/Resources/runtime'
verify(parent, release['manifest_sha256'])
allowed = {'services/api/app/schemas.py', 'services/api/app/services.py'}
trees = [('services/api/app', 'backend/app'), ('services/worker', 'backend/services/worker'),
         ('services/ai_gateway', 'backend/services/ai_gateway')]
for directory, target in trees:
    current = {str(p.relative_to(root / directory)): p for p in (root / directory).rglob('*.py') if '__pycache__' not in p.parts}
    bundled = {str(p.relative_to(parent / target)): p for p in (parent / target).rglob('*.py')}
    assert current.keys() == bundled.keys(), 'Unexpected backend file additions/removals'
    for name, path in current.items():
        if str(path.relative_to(root)) not in allowed:
            assert digest(path) == digest(bundled[name]), 'Unrelated source change: ' + str(path)
runtime = root / 'apps/macos/.build/runtime/search-20261022-final'
assert not runtime.exists()
subprocess.run(['cp', '-cR', str(parent), str(runtime)], check=True)
for directory, target in trees:
    for path in (root / directory).rglob('*.py'):
        if '__pycache__' in path.parts: continue
        shutil.copyfile(path, runtime / target / path.relative_to(root / directory))
manifest = runtime / 'manifest.json'
data = json.loads(manifest.read_text())
manifest.unlink()
data['parent_manifest_sha256'] = release['manifest_sha256']
data['scope'] = 'Exact literal search and source-scoped search; semantic default remains compatible'
data['files'] = tree_manifest(runtime)
manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
checksum = digest(manifest)
count = verify(runtime, checksum)
old = {entry['path']: entry for entry in json.loads((parent / 'manifest.json').read_text())['files']}
changes = [entry['path'] for entry in data['files'] if old.get(entry['path']) != entry]
assert set(changes) == {'backend/app/schemas.py', 'backend/app/services.py'}, changes
record = dict(schema_version=16, plugin_version='1.0.5', runtime=str(runtime.relative_to(root)),
              app='apps/macos/.build/search-final-candidate/知识收集箱.app',
              manifest_sha256=checksum, parent_manifest_sha256=release['manifest_sha256'],
              previous_source_record=release['source_record'], runtime_files=count, changed_runtime_files=changes,
              backend_sources={str(p.relative_to(root)): digest(p) for directory, _ in trees
                               for p in sorted((root / directory).rglob('*.py')) if '__pycache__' not in p.parts},
              tests={'status': 'verification_in_progress'})
(root / 'docs/verification/search-20261022/runtime-search.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'runtime': str(runtime), 'manifest_sha256': checksum, 'runtime_files': count}))
