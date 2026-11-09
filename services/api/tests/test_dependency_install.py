import base64
import csv
import hashlib
from io import BytesIO, StringIO
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace
import zipfile

import pytest

from app import dependency_install as dependencies, extension_install, runtime_extensions
from app.dependency_native import compatible_bytes


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def fixture(tmp_path, *, extra=None, nested_metadata=False):
    runtime, root = tmp_path / 'runtime', tmp_path / 'device/extensions'
    runtime.mkdir()
    info = 'example-1.0.dist-info'
    payload = {'example.py': b'VALUE = 42\n', info + '/METADATA': b'Name: example\nVersion: 1.0\n'}
    if nested_metadata:
        payload['example/_vendor/nested-1.0.dist-info/RECORD'] = b'original vendored metadata\n'
    archive = tmp_path / 'example.whl'
    with zipfile.ZipFile(archive, 'w') as output:
        for name, data in payload.items():
            output.writestr(name, data)
        output.writestr(info + '/RECORD', b'installer replaces this metadata')
        if extra:
            entry = zipfile.ZipInfo(extra)
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
            output.writestr(entry, '/tmp/outside')
    payload.update({info + '/INSTALLER': b'uv', info + '/REQUESTED': b'', info + '/RECORD': b''})
    record = StringIO(newline='')
    writer = csv.writer(record, lineterminator='\n')
    for name, data in sorted(payload.items()):
        encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')
        writer.writerow([name, '', ''] if name == info + '/RECORD' else [name, 'sha256=' + encoded, str(len(data))])
    payload[info + '/RECORD'] = record.getvalue().encode()
    manifest = dict(version=1, id='documents', files=[dict(path='packages/' + name, bytes=len(data),
        sha256=checksum(data), mode=0o644) for name, data in sorted(payload.items())])
    manifest_file = runtime / 'documents.json'
    manifest_file.write_text(json.dumps(manifest))
    artifact = dict(kind='wheel', filename='example.whl', bytes=archive.stat().st_size,
        sha256=checksum(archive.read_bytes()), urls=['https://pypi.tuna.tsinghua.edu.cn/packages/example.whl'])
    item = dict(id='documents', title='文档解析', summary='test', version='1.0', bytes=123,
        installed_bytes=sum(len(data) for data in payload.values()) + manifest_file.stat().st_size,
        sha256='a' * 64, manifest_sha256=checksum(manifest_file.read_bytes()),
        dependency_download=dict(version=1, manifest='documents.json', artifacts=[artifact]))
    value = dict(version=1, python=f'{sys.version_info.major}.{sys.version_info.minor}', architecture=platform.machine(),
        packages={key: {**item, 'id': key} for key in runtime_extensions.IDS})
    (runtime / 'extension-catalog.json').write_text(json.dumps(value))
    return runtime, root, manifest_file, archive, artifact, item


def test_wheel_reproduces_exact_approved_manifest_and_normalized_metadata(tmp_path):
    runtime, root, manifest, archive, artifact, item = fixture(tmp_path)
    destination = tmp_path / 'payload'
    dependencies.materialize(manifest, [(artifact, archive)], destination)
    runtime_extensions.verify_package(destination, item)
    assert (destination / 'packages/example.py').read_text() == 'VALUE = 42\n'
    assert (destination / 'packages/example-1.0.dist-info/INSTALLER').read_bytes() == b'uv'


def test_wheel_keeps_nested_vendor_record_as_unchanged_payload(tmp_path):
    _, _, manifest, archive, artifact, item = fixture(tmp_path, nested_metadata=True)
    destination = tmp_path / 'payload'
    dependencies.materialize(manifest, [(artifact, archive)], destination)
    runtime_extensions.verify_package(destination, item)
    assert (destination / 'packages/example/_vendor/nested-1.0.dist-info/RECORD').read_bytes() == b'original vendored metadata\n'


@pytest.mark.parametrize('extra', ['../../outside', 'packages/link'])
def test_wheel_rejects_traversal_and_links_even_if_not_in_output_manifest(tmp_path, extra):
    _, _, manifest, archive, artifact, _ = fixture(tmp_path, extra=extra)
    with pytest.raises(ValueError, match='不安全|特殊文件'):
        dependencies.materialize(manifest, [(artifact, archive)], tmp_path / 'payload')
    assert not (tmp_path / 'outside').exists()


def test_corrupt_input_never_materializes(tmp_path):
    _, _, manifest, archive, artifact, _ = fixture(tmp_path)
    archive.write_bytes(b'wrong')
    with pytest.raises(ValueError, match='校验失败'):
        dependencies.materialize(manifest, [(artifact, archive)], tmp_path / 'payload')


def test_output_must_match_previously_approved_bytes(tmp_path):
    _, _, manifest, archive, artifact, _ = fixture(tmp_path)
    data = json.loads(manifest.read_text())
    data['files'][0]['sha256'] = 'b' * 64
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='不一致'):
        dependencies.materialize(manifest, [(artifact, archive)], tmp_path / 'payload')


def test_install_is_atomic_and_retains_legacy_package_identity(tmp_path, monkeypatch):
    runtime, root, _, archive, _, item = fixture(tmp_path)
    monkeypatch.setattr(dependencies, 'fetch_artifact', lambda item, path, *args, **kwargs: path.write_bytes(archive.read_bytes()))
    monkeypatch.setattr(dependencies, 'health_check', lambda *args: None)
    states = []
    dependencies.install_dependencies(runtime, root, 'documents', component_progress=lambda *args: states.append(args))
    assert states == [(0, 'downloading', 0), (0, 'verified', 1), (0, 'completed', 1)]
    assert runtime_extensions.active(root) == {'documents': item['sha256']}
    runtime_extensions.verify_package(runtime_extensions.package_directory(root, item), item)
    assert extension_install.snapshot(runtime, root)['packages'][2]['online_available']
    assert list((root / 'staging').iterdir()) == []
    assert (root / 'active.json').stat().st_mode & 0o777 == 0o600


def test_health_check_cannot_mutate_accepted_payload(tmp_path, monkeypatch):
    runtime, root, _, archive, _, _ = fixture(tmp_path)
    monkeypatch.setattr(dependencies, 'fetch_artifact', lambda item, path, *args, **kwargs: path.write_bytes(archive.read_bytes()))
    monkeypatch.setattr(dependencies, 'health_check', lambda path, *args: (path / 'unexpected').write_text('changed'))
    with pytest.raises(ValueError, match='未记录'):
        dependencies.install_dependencies(runtime, root, 'documents')
    assert not (root / 'active.json').exists()


def test_native_compatibility_is_pinned_and_does_not_change_input():
    source, target = b'123456', b'12AB56XYZ'
    recipe = dict(source_bytes=len(source), source_sha256=checksum(source), target_bytes=len(target),
        target_sha256=checksum(target), blocks=[dict(offset=2, data=base64.b64encode(b'AB').decode()),
                                             dict(offset=6, data=base64.b64encode(b'XYZ').decode())])
    expected = dict(bytes=len(target), sha256=checksum(target))
    assert compatible_bytes(source, recipe, expected) == target
    assert source == b'123456'
    with pytest.raises(ValueError, match='不匹配'):
        compatible_bytes(b'wrong!', recipe, expected)
    recipe['blocks'][1]['offset'] = 3
    with pytest.raises(ValueError, match='范围'):
        compatible_bytes(source, recipe, expected)


@pytest.mark.parametrize('link', [False, True])
def test_archive_only_extracts_named_verified_model_file(tmp_path, link):
    _, _, manifest_file, wheel, wheel_item, item = fixture(tmp_path)
    data = b'verified model'
    manifest = json.loads(manifest_file.read_text())
    manifest['files'].append(dict(path='model/weights', bytes=len(data), sha256=checksum(data), mode=0o644))
    manifest_file.write_text(json.dumps(manifest))
    archive = tmp_path / 'model.tar'
    with tarfile.open(archive, 'w') as output:
        entry = tarfile.TarInfo('source/weights')
        entry.size = len(data)
        if link:
            entry.type, entry.linkname = tarfile.SYMTYPE, '/tmp/outside'
        output.addfile(entry, BytesIO(data))
    artifact = dict(kind='tar', bytes=archive.stat().st_size, sha256=checksum(archive.read_bytes()),
        members=[dict(member='source/weights', path='model/weights')])
    destination = tmp_path / 'payload'
    if link:
        with pytest.raises(ValueError, match='类型或大小'):
            dependencies.materialize(manifest_file, [(wheel_item, wheel), (artifact, archive)], destination)
    else:
        dependencies.materialize(manifest_file, [(wheel_item, wheel), (artifact, archive)], destination)
        assert (destination / 'model/weights').read_bytes() == data


def test_component_list_uses_stable_digest_ids_and_friendly_titles():
    artifact = dict(sha256='a' * 64, title='paddleocr', version='3.7.0', bytes=123, kind='wheel')
    result = dependencies.component_list({'dependency_download': {'artifacts': [artifact]}})
    assert result == [dict(id='a' * 64, title='文字识别引擎', detail='paddleocr · 3.7.0', bytes=123)]


@pytest.mark.parametrize('failure', ['download', 'health', 'cancel'])
def test_failure_does_not_activate_or_replace_previous_selection(tmp_path, monkeypatch, failure):
    runtime, root, _, archive, _, item = fixture(tmp_path)
    with extension_install.mutation(root):
        extension_install.write_json(root / 'active.json', {'version': 1, 'packages': {'semantic': 'c' * 64}})
    before = (root / 'active.json').read_bytes()
    def fetch(item, path, *args, **kwargs):
        path.write_bytes(archive.read_bytes())
        if failure == 'download':
            raise ValueError('模拟下载失败')
    def check(*args):
        if failure == 'health':
            raise ValueError('模拟自检失败')
    def progress(phase, fraction):
        if failure == 'cancel' and phase == 'activating':
            raise InterruptedError('用户取消')
    monkeypatch.setattr(dependencies, 'fetch_artifact', fetch)
    monkeypatch.setattr(dependencies, 'health_check', check)
    with pytest.raises((ValueError, InterruptedError)):
        dependencies.install_dependencies(runtime, root, 'documents', progress)
    assert (root / 'active.json').read_bytes() == before
    assert not runtime_extensions.package_directory(root, item).exists()
    assert list((root / 'staging').iterdir()) == []


def test_download_fails_over_only_to_pinned_sources_and_checks_content(tmp_path, monkeypatch):
    content, calls = b'public wheel', []
    class Response(BytesIO):
        status = 200
        url = 'https://mirrors.aliyun.com/packages/example.whl'
    def open_request(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise OSError('primary unavailable')
        return Response(content)
    monkeypatch.setattr(dependencies, 'build_opener', lambda *args: SimpleNamespace(open=open_request))
    item = dict(bytes=len(content), sha256=checksum(content), urls=[
        'https://pypi.tuna.tsinghua.edu.cn/packages/example.whl', Response.url])
    target = tmp_path / 'download'
    dependencies.fetch_artifact(item, target, lambda *args: None)
    assert target.read_bytes() == content and calls == item['urls']
    calls.clear()
    item['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='暂时不可用'):
        dependencies.fetch_artifact(item, target, lambda *args: None)
    assert not target.exists()


def test_cancel_does_not_fall_back_or_leave_partial_file(tmp_path, monkeypatch):
    class Response(BytesIO):
        status = 200
        url = 'https://pypi.tuna.tsinghua.edu.cn/packages/example.whl'
    monkeypatch.setattr(dependencies, 'build_opener', lambda *args: SimpleNamespace(open=lambda *args, **kwargs: Response(b'123')))
    def cancel(*args): raise InterruptedError('cancel')
    target = tmp_path / 'download'
    with pytest.raises(InterruptedError):
        dependencies.fetch_artifact(dict(bytes=3, sha256=checksum(b'123'), urls=[Response.url]), target, cancel)
    assert not target.exists()


@pytest.mark.parametrize('url', ['http://pypi.tuna.tsinghua.edu.cn/file', 'https://unknown.example/file',
    'https://user:pass@modelscope.cn/file', 'https://modelscope.cn:8080/file', 'https://modelscope.cn/file#fragment'])
def test_download_url_is_not_user_controlled(url):
    with pytest.raises(ValueError):
        dependencies.checked_url(url)


def test_ocr_cannot_use_unverified_dependency_route(tmp_path):
    runtime, root, *_ = fixture(tmp_path)
    with pytest.raises(ValueError, match='仅支持离线'):
        dependencies.install_dependencies(runtime, root, 'ocr')
    assert not root.exists()


def test_isolated_cli_loads_dependency_route_without_relative_import_error(tmp_path):
    runtime, root, *_ = fixture(tmp_path)
    value = json.loads((runtime / 'extension-catalog.json').read_text())
    value['packages']['documents']['dependency_download']['artifacts'][0]['urls'] = []
    (runtime / 'extension-catalog.json').write_text(json.dumps(value))
    source = Path(extension_install.__file__).parent
    target = runtime / 'backend/app'
    target.mkdir(parents=True)
    for name in ['extension_install.py', 'dependency_install.py', 'runtime_extensions.py']:
        shutil.copyfile(source / name, target / name)
    shutil.copytree(source / 'platform_support', target / 'platform_support', ignore=shutil.ignore_patterns('__pycache__'))
    result = subprocess.run([sys.executable, '-I', '-B', str(target / 'extension_install.py'),
        '--root', str(root), 'download', 'documents'], capture_output=True, text=True, timeout=20)
    assert result.returncode == 1 and '暂时不可用' in json.loads(result.stdout)['error']
    assert 'ImportError' not in result.stderr and not (root / 'active.json').exists()
