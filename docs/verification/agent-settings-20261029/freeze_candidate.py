"""Preserve the accepted pending OCR change and freeze only this Agent/settings scope."""
from pathlib import Path
import json
import shutil
import hashlib
import zipfile
import sys

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).parent
sys.path.insert(0, str(ROOT / 'apps/macos/scripts/runtime'))
from verify_runtime import digest, verify
from build_runtime import tree_manifest

base = json.loads((ROOT / 'docs/verification/ocr-dependencies-20261027/runtime-ocr-dependencies.json').read_text())
original = ROOT / base['app'] / 'Contents/Resources/runtime'
assert verify(original, base['manifest_sha256']) == base['runtime_files']
runtime = ROOT / 'apps/macos/.build/agent-settings-runtime'
if not runtime.exists():
    shutil.copytree(original, runtime, symlinks=True)
backend = dict(base['backend_sources'])
for name in ['agent_install.py', 'auth.py', 'main.py', 'schemas.py', 'services.py', 'agent_connections.py']:
    shutil.copy2(ROOT / 'services/api/app' / name, runtime / 'backend/app' / name)
    backend['services/api/app/' + name] = digest(runtime / 'backend/app' / name)
snapshot = ROOT / 'apps/macos/.build/agent-settings-source'
if not snapshot.exists():
    shutil.copytree(ROOT / base['source_snapshot'], snapshot, ignore=shutil.ignore_patterns('.build'))
sources = dict(base['sources'])
for name in ['LocalAgentInstaller.swift', 'LocalAgentConnections.swift', 'AgentLibraryPicker.swift', 'SettingsTabs.swift', 'Workspace.swift', 'LocalAI.swift']:
    shutil.copy2(ROOT / 'apps/macos/Sources' / name, snapshot / 'Sources' / name)
    sources['apps/macos/Sources/' + name] = digest(snapshot / 'Sources' / name)
for name, wanted in sources.items():
    assert digest(snapshot / 'Sources' / Path(name).name) == wanted
# Agent setup is a base feature. Reuse the exact accepted parser files; do not
# resolve or download new versions, and preserve old optional-package identities.
archive = ROOT / 'apps/macos/dist/扩展包/semantic-1.0.0-macos-arm64.pkaext'
item = json.loads((runtime / 'extension-catalog.json').read_text())['packages']['semantic']
assert digest(archive) == item['sha256']
with zipfile.ZipFile(archive) as package:
    raw = package.read('manifest.json')
    assert hashlib.sha256(raw).hexdigest() == item['manifest_sha256']
    parser_files = [entry for entry in json.loads(raw)['files']
        if entry['path'].startswith(('packages/tomli/', 'packages/tomli-2.4.1.dist-info/'))]
    assert len(parser_files) == 11
    for entry in parser_files:
        data = package.read(entry['path'])
        assert len(data) == entry['bytes'] and hashlib.sha256(data).hexdigest() == entry['sha256']
        target = runtime / 'python/lib/python3.10/site-packages' / entry['path'].removeprefix('packages/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(entry['mode'])
manifest = runtime / 'manifest.json'
value = json.loads(manifest.read_text())
value['files'] = [item for item in tree_manifest(runtime) if item['path'] != 'manifest.json']
manifest.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
result = dict(manifest_sha256=digest(manifest), runtime_files=verify(runtime, digest(manifest)),
    previous_release=json.loads((ROOT / 'apps/macos/release.json').read_text()),
    includes_pending_ocr_record='docs/verification/ocr-dependencies-20261027/runtime-ocr-dependencies.json',
    source_snapshot=str(snapshot.relative_to(ROOT)), sources=sources, backend_sources=backend,
    excluded_concurrent_changes=['services/api/app/browser_native.py'],
    capability_roots_sha256=digest(ROOT / 'apps/macos/runtime/capability-roots.json'))
(HERE / 'candidate-inputs.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print('PASS: frozen Agent settings, pending OCR preserved;', result['runtime_files'], 'runtime entries')
