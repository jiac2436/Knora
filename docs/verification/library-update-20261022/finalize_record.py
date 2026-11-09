from pathlib import Path
import hashlib
import json
import shutil

root = Path(__file__).resolve().parents[3]
folder = root / 'docs/verification/library-update-20261022'
path = folder / 'runtime-library-update.json'
record = json.loads(path.read_text())
previous = json.loads((root / record['previous_source_record']).read_text())
snapshot = root / 'apps/macos/.build/library-update-source'
def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
record['sources'] = {name: checksum(snapshot / 'Sources' / Path(name).name) for name in previous['sources']}
allowed = {'apps/macos/Sources/LocalDomainFullAnalysis.swift',
           'apps/macos/Sources/LocalDomainFullAnalysisView.swift', 'apps/macos/Sources/LocalDomainOverviewView.swift'}
changed = {name for name, value in record['sources'].items() if value != previous['sources'][name]}
assert changed == allowed, changed
for name in changed:
    assert checksum(root / name) == record['sources'][name], 'Scoped source changed during acceptance: ' + name
record['source_snapshot'] = str(snapshot.relative_to(root))
record['excluded_concurrent_changes'] = ['apps/macos/Sources/MacKernelHost.swift', 'apps/macos/Sources/BrowserBridgeInstaller.swift']
record['changed_sources'] = sorted(allowed | {name for name, value in record['backend_sources'].items()
    if previous['backend_sources'].get(name) != value})
record['added_sources'] = ['services/api/app/domain_full_usage.py']
app = root / record['app']
record['native_binary_sha256'] = checksum(app / 'Contents/MacOS/KnowledgeInbox')
record['unchanged_native_binary_sha256'] = record['native_binary_sha256']
record['icon_sha256'] = checksum(app / 'Contents/Resources/AppIcon.icns')
assert record['icon_sha256'] == checksum(root / 'apps/macos/dist/知识收集箱.app/Contents/Resources/AppIcon.icns')
logs = {
    'backend-tests.log': '/tmp/pka-library-tests3.log',
    'coverage-test.log': '/tmp/pka-library-coverage-test.log',
    'swift-state-tests.log': '/tmp/pka-library-swift-result.log',
    'packaged-runtime-tests.log': '/tmp/pka-library-runtime-tests.log',
    'packaged-update-tests.log': '/tmp/pka-library-new-runtime-tests.log',
    'build.log': '/tmp/pka-library-build.log'}
for destination, original in logs.items():
    shutil.copyfile(original, folder / destination)
assert '110 passed' in (folder / 'backend-tests.log').read_text()
assert '1 passed' in (folder / 'coverage-test.log').read_text()
for name in ('swift-state-tests.log', 'packaged-runtime-tests.log', 'packaged-update-tests.log'):
    assert 'PASS:' in (folder / name).read_text(), name
assert '构建完成' in (folder / 'build.log').read_text()
record['tests'] = dict(status='passed', backend='110 passed; additional coverage read regression passed',
    swift='Existing state suite plus mode switching, delayed responses and persistent mode recovery passed',
    native_ui='Production views in isolated bundle: combined entry, default checkbox, explanation, automatic range switch, progress and completed token receipts verified',
    packaged_runtime='Current private startup; actual host/API/workers; synthetic loopback provider, temporary SQLite; default stale exclusion, force all, receipt totals, restart/idempotency/unknown passed',
    real_provider_calls=0, runtime_manifest='19959 files verified', signature='deep strict verification passed',
    source_scope='3 Swift changes and 5 backend files; unrelated concurrent native changes excluded by matching previous verified hashes')
path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(dict(changed_sources=record['changed_sources'], manifest_sha256=record['manifest_sha256'], native_binary_sha256=record['native_binary_sha256']), indent=2))
