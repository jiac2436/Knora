"""Freeze this task's native changes and the exact previous approved resources."""
from pathlib import Path
import hashlib
import json
import shutil
ROOT = Path(__file__).resolve().parents[3]
release = json.loads((ROOT / 'apps/macos/release.json').read_text())
record = json.loads((ROOT / release['source_record']).read_text())
output = ROOT / 'apps/macos/.build/lightweight-source'
output.mkdir(parents=True, exist_ok=True)
source = output / 'Sources'
source.mkdir(exist_ok=True)
allowed = {'Model.swift', 'Knowledge.swift', 'MacKernelHost.swift', 'Workspace.swift',
           'LocalStorage.swift', 'StoragePresentation.swift'}
previous = ROOT / record['source_snapshot'] / 'Sources'
sources = {}
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()
for name, wanted in record['sources'].items():
    current = ROOT / name
    chosen = current
    if current.name not in allowed and digest(current) != wanted:
        chosen = previous / current.name
        assert digest(chosen) == wanted, str(chosen)
    target = source / current.name
    shutil.copy2(chosen, target)
    sources[name] = digest(target)
new = ROOT / 'apps/macos/Sources/LocalExtensions.swift'
shutil.copy2(new, source / new.name)
sources[str(new.relative_to(ROOT))] = digest(new)
for path in source.glob('*.swift'):
    assert 'apps/macos/Sources/' + path.name in sources
resources = output / 'Resources'
resources.mkdir(exist_ok=True)
if not (resources / 'Capture').exists():
    shutil.copytree(ROOT / release['app'] / 'Contents/Resources/Capture', resources / 'Capture')
(resources / 'Brand').mkdir(exist_ok=True)
shutil.copy2(ROOT / release['app'] / 'Contents/Resources/AppIcon.icns', resources / 'Brand/AppIcon.icns')
(output / 'scripts/runtime').mkdir(parents=True, exist_ok=True)
for name in ['scripts/build.sh', 'scripts/runtime/verify_runtime.py']:
    shutil.copy2(ROOT / 'apps/macos' / name, output / name)
(Path(__file__).parent / 'native-snapshot.json').write_text(json.dumps(sources, ensure_ascii=False, indent=2)+'\n')
print('PASS: native snapshot',len(sources),'files; existing approved icon/capture resources retained')
