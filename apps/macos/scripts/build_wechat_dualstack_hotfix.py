"""Build a scoped backend hotfix from the verified joint-image application.

The native executable and every other runtime file retain the delivered
baseline. This deliberately excludes unrelated work in the current checkout.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'apps/macos/scripts/runtime'))
from build_runtime import tree_manifest
from verify_runtime import digest, verify


def main():
    record = json.loads((ROOT / 'docs/verification/a1/runtime-joint-images-20261017.json').read_text())
    expected_manifest = '48730672003f92c208f2b8dde244d22221ed041b67fa63059102ff573f4af6b1'
    assert record['manifest_sha256'] == expected_manifest
    parent, original_app = ROOT / record['runtime'], ROOT / record['app']
    verify(parent, expected_manifest)
    verify(original_app / 'Contents/Resources/runtime', expected_manifest)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(original_app)], check=True)

    relative = 'backend/services/worker/web_reader.py'
    original = (parent / relative).read_text()
    replacements = [
        ('FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")',
         'FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("2001:2::/48"))'),
        ('and ip in FAKE_IP_NETWORK)', 'and any(ip in network for network in FAKE_IP_NETWORKS))'),
        ('if denied and parts.scheme == "https" and all(ip in FAKE_IP_NETWORK for ip in denied):',
         'if denied and parts.scheme == "https" and all(\n            any(ip in network for network in FAKE_IP_NETWORKS) for ip in denied):'),
    ]
    expected = original
    for before, after in replacements:
        assert expected.count(before) == 1
        expected = expected.replace(before, after, 1)
    patched = ROOT / 'services/worker/web_reader.py'
    assert patched.read_text() == expected, 'Worker differs beyond the reviewed dual-stack fix'

    runtime = ROOT / 'apps/macos/.build/runtime/rebuilt-20261019-v16-wechat-dualstack-verified'
    app = ROOT / 'apps/macos/dist/知识收集箱-v16-微信双栈修复.app'
    assert not runtime.exists() and not app.exists(), 'Outputs already exist; do not overwrite'
    subprocess.run(['cp', '-cR', str(parent), str(runtime)], check=True)
    shutil.copyfile(patched, runtime / relative)
    manifest_path = runtime / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest_path.unlink()
    manifest['parent_manifest_sha256'] = expected_manifest
    manifest['scope'] = 'IPv4/IPv6 Fake-IP HTTPS opt-in; one backend file only'
    manifest['files'] = tree_manifest(runtime)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    checksum = digest(manifest_path)
    count = verify(runtime, checksum)

    subprocess.run(['cp', '-cR', str(original_app), str(app)], check=True)
    shutil.rmtree(app / 'Contents/Resources/runtime')
    subprocess.run(['cp', '-cR', str(runtime), str(app / 'Contents/Resources/runtime')], check=True)
    binary = 'Contents/MacOS/KnowledgeInbox'
    assert digest(original_app / binary) == digest(app / binary)
    subprocess.run(['codesign', '--force', '--sign', '-', str(app)], check=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    verify(app / 'Contents/Resources/runtime', checksum)
    result = dict(schema_version=16, plugin_version='1.0.5', runtime=str(runtime.relative_to(ROOT)),
        app=str(app.relative_to(ROOT)), manifest_sha256=checksum, runtime_files=count,
        parent_manifest_sha256=expected_manifest, base_record='runtime-joint-images-20261017.json',
        unchanged_native_binary_sha256=digest(app / binary),
        changed_runtime_files={relative: digest(patched)},
        test_source_sha256=digest(ROOT / 'services/api/tests/test_web_read.py'),
        build_script_sha256=digest(Path(__file__)), tests={'status': 'verification_in_progress'})
    (ROOT / 'docs/verification/a1/runtime-wechat-dualstack-20261019.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'app': result['app'], 'manifest_sha256': checksum, 'files': count}, ensure_ascii=False))


if __name__ == '__main__':
    main()
