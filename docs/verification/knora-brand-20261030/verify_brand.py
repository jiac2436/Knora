"""Check the delivered brand and compatibility without opening user libraries."""
import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'apps/macos/scripts/runtime'))
from verify_runtime import verify


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    record = json.loads((Path(__file__).parent / 'runtime-knora-brand.json').read_text())
    app = Path(sys.argv[1]).resolve(strict=True)
    with (app / 'Contents/Info.plist').open('rb') as handle:
        info = plistlib.load(handle)
    assert app.name == 'Knora.app'
    assert info['CFBundleName'] == 'Knora'
    assert info['CFBundleDisplayName'] == '知径 · Knora'
    assert info['CFBundleIdentifier'] == 'local.pka.knowledge-inbox'
    assert info['CFBundleExecutable'] == 'KnowledgeInbox'
    assert info['CFBundleURLTypes'][0]['CFBundleURLSchemes'] == ['knowledge-inbox']
    assert info['CFBundleShortVersionString'] == '0.1.0'
    assert digest(app / 'Contents/Resources/AppIcon.icns') == record['icon_sha256']
    print('PASS: Knora application metadata; original identity, article URL scheme and A icon retained')
    snapshot = ROOT / record['source_snapshot']
    for path, expected in record['sources'].items():
        assert digest(snapshot / 'Sources' / Path(path).name) == expected, path
    previous = json.loads((ROOT / record['previous_release']).read_text())
    allowed = {'App.swift', 'Workspace.swift', 'LocalStorageView.swift', 'LocalAgentConnections.swift',
               'Model.swift', 'FeishuCLI.swift', 'FeishuSettings.swift'}
    changed = {Path(path).name for path, checksum in previous['sources'].items()
               if record['sources'][path] != checksum}
    assert changed == allowed
    assert set(record['sources']) - set(previous['sources']) == {'apps/macos/Sources/AppBrand.swift'}
    for name in ['MacPaths.swift', 'MacDeviceCredentialStore.swift', 'MacModelCredentialStore.swift',
                 'MacKernelHost.swift', 'ReadingSession.swift']:
        path = 'apps/macos/Sources/' + name
        assert record['sources'][path] == previous['sources'][path]
    print('PASS: 80 frozen native source hashes; data, credentials, reading-state paths and host logic unchanged')
    runtime = app / 'Contents/Resources/runtime'
    assert verify(runtime, record['manifest_sha256']) == 3022
    manifest = json.loads((runtime / 'manifest.json').read_text())
    assert manifest['parent_manifest_sha256'] == previous['manifest_sha256']
    prior_runtime = ROOT / previous['app'] / 'Contents/Resources/runtime'
    old = json.loads((prior_runtime / 'manifest.json').read_text())
    original = {entry['path']: entry for entry in old['files']}
    modified = {entry['path'] for entry in manifest['files'] if entry != original[entry['path']]}
    assert modified == {'backend/app/article_links.py', 'backend/app/agent_plugin.py'}
    print('PASS: 3022 runtime entries; only article handoff and plugin developer branding changed')
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    print('PASS: deep strict signature')


if __name__ == '__main__':
    main()
