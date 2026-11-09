"""Verify the exact candidate, source pins and completed acceptance evidence."""
from pathlib import Path
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).parent
sys.path.insert(0, str(ROOT / 'apps/macos/scripts/runtime'))
from verify_runtime import digest, verify

inputs = json.loads((HERE / 'candidate-inputs.json').read_text())
release = json.loads((ROOT / 'apps/macos/release.json').read_text())
assert release['manifest_sha256'] == inputs['previous_release']['manifest_sha256'], '正式版已变化，需重新检查基线'
baseline = json.loads((ROOT / release['source_record']).read_text())
app = ROOT / 'apps/macos/.build/dependency-candidate/知识收集箱.app'
runtime = app / 'Contents/Resources/runtime'
assert verify(runtime, inputs['manifest_sha256']) == inputs['runtime_files'] == 3007
subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
assert digest(app / 'Contents/Resources/AppIcon.icns') == baseline['icon_sha256']
for key, wanted in inputs['sources'].items():
    assert digest(ROOT / inputs['source_snapshot'] / 'Sources' / Path(key).name) == wanted, key
for key, wanted in inputs['backend_sources'].items():
    path = Path(key)
    relative = Path(*path.parts[2:]) if path.parts[1] == 'api' else Path('services', *path.parts[1:])
    assert digest(runtime / 'backend' / relative) == wanted, key
for name, expected in [('backend-tests.log', '1478 passed, 1 skipped'),
    ('real-download-install.log', 'PASS: restart activates downloaded engines'),
    ('base-cloud.log', 'PASS: bundled cloud executor'), ('library-update.log', 'PASS:'),
    ('extensions-ui.txt', '语义搜索 等待重启启用')]:
    assert expected in (HERE / name).read_text(), name
ui = (HERE / 'extensions-ui.txt').read_text()
assert '文档解析 等待重启启用' in ui and '仅支持离线安装' in ui
assert '下载服务器' not in ui and 'text field' not in ui
audit = json.loads((HERE / 'wheel-audit.json').read_text())
assert len(audit['wheels']) == 66 and all(item.get('download_verified') for item in audit['wheels'])
assert all(item['verified'] for item in json.loads((HERE / 'model-audit.json').read_text()))
assert len(json.loads((HERE / 'secondary-source-audit.json').read_text())) == 3
catalog = json.loads((runtime / 'extension-catalog.json').read_text())
for key, item in catalog['packages'].items():
    assert item['sha256'] == baseline['extensions']['packages'][key]['sha256']
    assert digest(ROOT / 'apps/macos/dist/扩展包' / item['filename']) == item['sha256']
    if key == 'ocr':
        assert 'dependency_download' not in item and item['online_reason']
    else:
        assert digest(runtime / item['dependency_download']['manifest']) == item['manifest_sha256']
binary = digest(app / 'Contents/MacOS/KnowledgeInbox')
files = [path for path in app.rglob('*') if path.is_file() and not path.is_symlink()]
record = dict(schema_version=16, plugin_version='1.0.5', kernel_contract_version=1,
    app=str(app.relative_to(ROOT)), manifest_sha256=inputs['manifest_sha256'], runtime_files=inputs['runtime_files'],
    native_binary_sha256=binary, unchanged_native_binary_sha256=binary,
    icon_sha256=digest(app / 'Contents/Resources/AppIcon.icns'), previous_release=release,
    previous_source_record=release['source_record'], sources=inputs['sources'], backend_sources=inputs['backend_sources'],
    source_snapshot=inputs['source_snapshot'], excluded_concurrent_changes=inputs['excluded_concurrent_changes'],
    changed_sources=sorted([key for key, value in inputs['sources'].items() if value != baseline['sources'].get(key)] +
                           [key for key, value in inputs['backend_sources'].items() if value != baseline['backend_sources'].get(key)]),
    extensions=catalog, application_logical_bytes=sum(path.stat().st_size for path in files),
    application_allocated_bytes=sum(path.stat().st_blocks * 512 for path in files),
    acceptance_file_sha256={path.name: digest(path) for path in HERE.iterdir() if path.suffix in {'.log', '.png', '.txt'}},
    tests=dict(status='passed; ready for publication', backend='1478 passed, 1 skipped, 1 warning',
        sources='66 locked wheels downloaded from TUNA with hashes verified; Aliyun exact listings and 3 complete secondary downloads; 7 identical ModelScope model files and 2 Paddle BOS archives verified',
        runtime='Real candidate interpreter, domestic downloads and exact old output manifests; native/model health, atomic installation; running host unaffected, restart enables real semantic retrieval',
        cloud_ai='Base/MCP/cloud execution/library update regression with synthetic model replies; no real provider calls',
        native='Complete optimized build and strict signature; real production extension view installed documents and semantic in isolated home, progress and restart statuses verified, no download-source entry',
        scope='OCR remains offline by explicit user choice; legacy extension identities and schema16/plugin1.0.5 preserved; no user data or credential changes',
        real_provider_calls=0),
    known_limits=['Restart remains required after install/enable/disable/remove; no live native-library replacement',
        'OCR raw Mac wheel still needs native runtime repairs; online OCR intentionally not enabled',
        'Public mirrors have no commercial SLA; pinned fallback/retry and original offline packages remain',
        'macOS arm64 only; no clean-device commercial signing/notarization acceptance; no resumable downloads'])
(HERE / 'runtime-dependency-sources.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
print('PASS: candidate/source/icon/legacy extension identities and completed acceptance verified')
print('Application logical MB:', round(record['application_logical_bytes'] / 1e6, 2))
