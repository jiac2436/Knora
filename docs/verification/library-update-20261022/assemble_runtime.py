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
allowed = {'services/api/app/domain_full_analysis.py', 'services/api/app/domain_full_usage.py', 'services/api/app/domain_builds.py', 'services/api/app/domain_overview_plan.py', 'services/worker/domain_full_analysis.py'}
trees = [('services/api/app', 'backend/app'), ('services/worker', 'backend/services/worker'),
         ('services/ai_gateway', 'backend/services/ai_gateway')]
for directory, target in trees:
    current = {str(p.relative_to(root / directory)): p for p in (root / directory).rglob('*.py') if '__pycache__' not in p.parts}
    bundled = {str(p.relative_to(parent / target)): p for p in (parent / target).rglob('*.py')}
    assert not bundled.keys() - current.keys(), 'Unexpected backend removals'
    assert all(str(current[name].relative_to(root)) in allowed for name in current.keys() - bundled.keys()), 'Unexpected additions'
    for name, path in current.items():
        if str(path.relative_to(root)) not in allowed:
            assert digest(path) == digest(bundled[name]), 'Unrelated source change: ' + str(path)
runtime = root / 'apps/macos/.build/runtime/library-update-20261022-release'
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
data['scope'] = 'Reviewed library update: default skips stale analyses; explicit reanalysis processes all; complete provider token receipts only'
data['files'] = tree_manifest(runtime)
manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
checksum = digest(manifest)
count = verify(runtime, checksum)
old = {entry['path']: entry for entry in json.loads((parent / 'manifest.json').read_text())['files']}
changes = [entry['path'] for entry in data['files'] if old.get(entry['path']) != entry]
assert set(changes) == {'backend/app/domain_full_analysis.py', 'backend/app/domain_full_usage.py', 'backend/app/domain_builds.py', 'backend/app/domain_overview_plan.py', 'backend/services/worker/domain_full_analysis.py'}, changes
record = dict(schema_version=16, plugin_version='1.0.5', runtime=str(runtime.relative_to(root)),
              app='apps/macos/.build/library-update-candidate/知识收集箱.app',
              manifest_sha256=checksum, parent_manifest_sha256=release['manifest_sha256'],
              previous_source_record=release['source_record'], runtime_files=count, changed_runtime_files=changes,
              backend_sources={str(p.relative_to(root)): digest(p) for directory, _ in trees
                               for p in sorted((root / directory).rglob('*.py')) if '__pycache__' not in p.parts},
              tests={'status': 'verification_in_progress'})
(root / 'docs/verification/library-update-20261022/runtime-library-update.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'runtime': str(runtime), 'manifest_sha256': checksum, 'runtime_files': count}))
