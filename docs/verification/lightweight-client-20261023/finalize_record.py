"""Verify frozen artifacts and write the actual acceptance record."""
from pathlib import Path
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).parent
sys.path.insert(0, str(ROOT / 'apps/macos/scripts/runtime'))
from verify_runtime import digest, verify

app = ROOT / 'apps/macos/.build/lightweight-candidate/知识收集箱.app'
base = app / 'Contents/Resources/runtime'
split = json.loads((ROOT / 'apps/macos/.build/lightweight-runtime-v1/split-result.json').read_text())
release = json.loads((ROOT / 'apps/macos/release.json').read_text())
baseline = json.loads((ROOT / release['source_record']).read_text())
count = verify(base, split['manifest_sha256'])
assert count == 3004
subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
assert digest(app / 'Contents/Resources/AppIcon.icns') == digest(ROOT / release['app'] / 'Contents/Resources/AppIcon.icns')
for name, expected in [('backend-tests.log', '1460 passed, 1 skipped'), ('base-cloud.log', 'PASS: bundled cloud executor'),
                       ('backup.log', 'PASS:'), ('library-update.log', 'PASS:'), ('real-extensions.log', 'PASS: uninstall'),
                       ('StorageTest.log', 'PASS:'), ('StoragePresentationTest.log', 'PASS:')]:
    assert expected in (HERE / name).read_text(), name
sources = json.loads((HERE / 'native-snapshot.json').read_text())
backend = json.loads((HERE / 'backend-snapshot.json').read_text())
for key, wanted in sources.items():
    assert digest(ROOT / 'apps/macos/.build/lightweight-source/Sources' / Path(key).name) == wanted, key
for key, wanted in backend.items():
    path = Path(key)
    relative = Path(*path.parts[2:]) if path.parts[1] == 'api' else Path('services', *path.parts[1:])
    assert digest(base / 'backend' / relative) == wanted, key
binary = digest(app / 'Contents/MacOS/KnowledgeInbox')
record = dict(schema_version=16, plugin_version='1.0.5', kernel_contract_version=1,
    manifest_sha256=split['manifest_sha256'], runtime_files=count, app=str(app.relative_to(ROOT)),
    previous_source_record=release['source_record'], previous_release=release,
    source_snapshot='apps/macos/.build/lightweight-source', sources=sources, backend_sources=backend,
    changed_sources=sorted([key for key, value in sources.items() if value != baseline['sources'].get(key)] +
                           [key for key, value in backend.items() if value != baseline['backend_sources'].get(key)]),
    excluded_concurrent_changes=json.loads((HERE / 'excluded-concurrent.json').read_text()),
    native_binary_sha256=binary,
    # Publisher's legacy key means the tested binary must match at publication.
    unchanged_native_binary_sha256=binary,
    icon_sha256=digest(app / 'Contents/Resources/AppIcon.icns'), extensions=split['catalog'],
    sizes=json.loads((HERE / 'sizes.json').read_text()),
    acceptance_file_sha256={p.name: digest(p) for p in HERE.iterdir() if p.suffix in ['.log', '.png', '.txt']},
    tests=dict(status='passed; ready for publication',
        backend='1460 passed, 1 skipped, 1 warning; full development API/Worker regression',
        base='Actual candidate Python/Host/Worker: startup without extensions, capture, exact search, MCP stdio and evidence, explicit missing semantic, restart persistence',
        cloud_ai='Actual candidate executor/validation/index pipeline and knowledge-library update without optional packages; synthetic provider responses',
        extensions='Three real offline archives installed; real offline embeddings, CSV conversion, Paddle Small pixel OCR; disable/uninstall preserve generated data',
        native='Optimized build and deep strict ad-hoc signature; real production extension view import/disable/enable/uninstall in isolated app; storage accounting/grouping passed',
        backup='Actual candidate CLI export/verify/restore; no replay of execution queue or model credentials',
        scope='Schema16/plugin1.0.5; approved A icon and resources retained; concurrent browser_native.py excluded',
        real_provider_calls=0),
    known_limits=['macOS arm64 only; no clean-device commercial signing/notarization acceptance',
        'No production CDN configured; HTTPS downloader tested with synthetic transport',
        'App-pinned packages; no independent remote catalog updates or resumable downloads',
        'Existing Host/Worker execution; restart required, no per-engine process isolation',
        'Default base has no optional extensions; manual installation; existing articles/results retained'])
(HERE / 'runtime-lightweight.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
print('PASS: final native/backend/icon/archive digests and acceptance logs verified; release record created')
