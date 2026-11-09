from dataclasses import replace
from io import BytesIO
import hashlib
import json
from pathlib import Path
import platform
import stat
import sys
from types import SimpleNamespace
import zipfile

import pytest
from sqlalchemy import select

from app import extension_install as installer, runtime_extensions as extensions
from app.database import SessionLocal
from app.models import DocumentJob, OCRJob, Source, SourceRevision


def checksum(data):
    return hashlib.sha256(data).hexdigest()


def package(tmp_path, *, entries=None, extra=None, architecture=None):
    runtime = tmp_path / 'runtime'
    runtime.mkdir(exist_ok=True)
    payload = entries or {'packages/example_capability.py': b'VALUE = 42\n'}
    manifest = json.dumps({'version': 1, 'id': 'semantic', 'files': [
        {'path': key, 'bytes': len(value), 'sha256': checksum(value)} for key, value in payload.items()
    ]}).encode()
    archive = tmp_path / 'semantic.pkaext'
    with zipfile.ZipFile(archive, 'w') as target:
        for key, value in payload.items():
            target.writestr(key, value)
        target.writestr('manifest.json', manifest)
        if extra:
            info = zipfile.ZipInfo(extra)
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            target.writestr(info, '/etc/passwd')
    item = {'id': 'semantic', 'version': '1.0.0', 'title': '语义搜索', 'summary': 'test',
            'bytes': archive.stat().st_size, 'installed_bytes': sum(map(len, payload.values())) + len(manifest),
            'sha256': checksum(archive.read_bytes()), 'manifest_sha256': checksum(manifest), 'filename': archive.name}
    value = {'version': 1, 'python': f'{sys.version_info.major}.{sys.version_info.minor}', 'architecture': architecture or platform.machine(),
             'packages': {key: {**item, 'id': key} for key in extensions.IDS}}
    (runtime / 'extension-catalog.json').write_text(json.dumps(value))
    return runtime, tmp_path / 'device/extensions', archive, item


def test_catalog_rejects_different_python_abi_before_install(tmp_path):
    runtime, root, archive, _ = package(tmp_path)
    path = runtime / 'extension-catalog.json'
    value = json.loads(path.read_text())
    value['python'] = '0.0'
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='不兼容'):
        installer.install(runtime, root, archive)
    assert not root.exists()


def test_offline_install_enable_disable_keeps_data_and_code(tmp_path):
    runtime, root, archive, item = package(tmp_path)
    unrelated = tmp_path / 'knowledge.txt'
    unrelated.write_text('保留正文')
    assert installer.install(runtime, root, archive) == 'semantic'
    path = extensions.package_directory(root, item)
    assert (path / 'packages/example_capability.py').read_text() == 'VALUE = 42\n'
    assert installer.snapshot(runtime, root)['packages'][0]['enabled']
    installer.change(runtime, root, 'semantic', False)
    assert not installer.snapshot(runtime, root)['packages'][0]['enabled']
    assert path.is_dir() and unrelated.read_text() == '保留正文'
    installer.change(runtime, root, 'semantic', True)
    assert extensions.active(root) == {'semantic': item['sha256']}
    assert (root / 'active.json').stat().st_mode & 0o777 == 0o600


def test_tampered_archive_cannot_replace_installed_selection(tmp_path):
    runtime, root, archive, item = package(tmp_path)
    installer.install(runtime, root, archive)
    saved = (root / 'active.json').read_bytes()
    content = bytearray(archive.read_bytes())
    content[-1] ^= 1
    archive.write_bytes(content)
    with pytest.raises(ValueError, match='校验失败'):
        installer.install(runtime, root, archive)
    assert (root / 'active.json').read_bytes() == saved
    extensions.verify_package(extensions.package_directory(root, item), item)


@pytest.mark.parametrize('name', ['../outside.py', '/tmp/outside.py', 'packages\\escape.py'])
def test_even_pinned_archive_cannot_traverse(tmp_path, name):
    runtime, root, archive, _ = package(tmp_path, entries={name: b'bad'})
    with pytest.raises(ValueError, match='不安全'):
        installer.install(runtime, root, archive)
    assert not (root / 'active.json').exists()
    assert not (tmp_path / 'outside.py').exists()


def test_links_and_unrecorded_files_rejected(tmp_path):
    runtime, root, archive, _ = package(tmp_path, extra='packages/symlink')
    with pytest.raises(ValueError):
        installer.install(runtime, root, archive)
    assert not (root / 'active.json').exists()


def test_symlink_install_directory_rejected(tmp_path):
    runtime, root, archive, _ = package(tmp_path)
    root.parent.mkdir()
    real = tmp_path / 'real'
    real.mkdir()
    root.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match='符号链接'):
        installer.install(runtime, root, archive)
    assert list(real.iterdir()) == []


def test_install_cancellation_preserves_selection(tmp_path):
    runtime, root, archive, _ = package(tmp_path)
    def cancel(phase, fraction):
        if phase == 'installing':
            raise InterruptedError('cancel')
    with pytest.raises(InterruptedError):
        installer.install(runtime, root, archive, cancel)
    assert not (root / 'active.json').exists()
    assert list((root / 'staging').iterdir()) == []


def test_mutations_are_serialized(tmp_path):
    runtime, root, archive, _ = package(tmp_path)
    with installer.mutation(root):
        with pytest.raises(ValueError, match='另一个'):
            installer.install(runtime, root, archive)


def test_uninstall_waits_for_running_process_and_keeps_user_data(tmp_path):
    runtime, root, archive, item = package(tmp_path)
    installer.install(runtime, root, archive)
    document = tmp_path / 'original.md'
    document.write_text('不可删除的原文')
    directory = extensions.package_directory(root, item)
    with extensions.package_lock(root, item):
        installer.remove(runtime, root, 'semantic')
        assert directory.is_dir()
        assert installer.snapshot(runtime, root)['packages'][0]['removal_pending']
        assert 'semantic' not in extensions.active(root)
    installer.collect_removals(runtime, root)
    assert not directory.exists()
    assert document.read_text() == '不可删除的原文'
    assert not installer.snapshot(runtime, root)['packages'][0]['removal_pending']


def test_enable_cancels_pending_uninstall(tmp_path):
    runtime, root, archive, item = package(tmp_path)
    installer.install(runtime, root, archive)
    with extensions.package_lock(root, item):
        installer.remove(runtime, root, 'semantic')
        installer.change(runtime, root, 'semantic', True)
    installer.collect_removals(runtime, root)
    assert extensions.package_directory(root, item).is_dir()
    assert extensions.active(root)['semantic'] == item['sha256']


def test_reinstall_repairs_corrupted_files(tmp_path):
    runtime, root, archive, item = package(tmp_path)
    installer.install(runtime, root, archive)
    directory = extensions.package_directory(root, item)
    (directory / 'packages/example_capability.py').write_text('corrupt')
    installer.install(runtime, root, archive)
    extensions.verify_package(directory, item)


@pytest.mark.parametrize('url', ['http://example.com', 'https://user:password@example.com',
    'file:///tmp/archive', 'https://example.com/?secret=1', 'https://example.com/#fragment'])
def test_download_source_rejects_unsafe_schemes_and_credentials(url):
    with pytest.raises(ValueError):
        installer.source_url(url)


def test_missing_source_does_not_download_or_change_active(tmp_path):
    runtime, root, _, _ = package(tmp_path)
    with pytest.raises(ValueError, match='尚未配置'):
        installer.download(runtime, root, 'semantic')
    assert not (root / 'active.json').exists()


@pytest.mark.parametrize('corrupt', [False, True])
def test_https_download_uses_pinned_bytes_and_atomic_install(tmp_path, monkeypatch, corrupt):
    import urllib.request
    runtime, root, archive, item = package(tmp_path)
    with installer.mutation(root):
        installer.write_json(root / 'download-source.json', {'url': 'https://downloads.example.test/extensions'})
    content = archive.read_bytes()
    if corrupt:
        content = content[:-1] + bytes([content[-1] ^ 1])
    seen = []
    class Response(BytesIO):
        status = 200
    def open_request(request, timeout):
        seen.append(request.full_url)
        return Response(content)
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: SimpleNamespace(open=open_request))
    if corrupt:
        with pytest.raises(ValueError, match='摘要不匹配'):
            installer.download(runtime, root, 'semantic')
        assert not (root / 'active.json').exists()
    else:
        installer.download(runtime, root, 'semantic')
        assert extensions.active(root)['semantic'] == item['sha256']
    assert seen == ['https://downloads.example.test/extensions/' + archive.name]
    assert list((root / 'staging').iterdir()) == []


def test_wrong_platform_does_not_install(tmp_path):
    runtime, root, archive, _ = package(tmp_path, architecture='unsupported-cpu')
    with pytest.raises(ValueError, match='不兼容'):
        installer.install(runtime, root, archive)
    assert not root.exists()


def test_base_activation_has_no_heavy_modules_and_bad_extension_is_nonfatal(tmp_path, monkeypatch):
    runtime, root, archive, item = package(tmp_path)
    monkeypatch.setattr(extensions, 'runtime_root', lambda: runtime)
    monkeypatch.setattr(extensions, 'extension_root', lambda: root)
    monkeypatch.setattr(extensions, '_loaded', None)
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.delenv('PKA_SEMANTIC_ENABLED', raising=False)
    monkeypatch.setenv('PKA_SEMANTIC_ENABLED', '1')
    extensions.activate()
    assert not extensions.available('semantic')
    assert extensions.os.environ['PKA_SEMANTIC_ENABLED'] == '0'
    installer.install(runtime, root, archive)
    # Selection is immutable for a running process; installation is next-launch.
    assert not extensions.available('semantic')
    extensions._leases.close()
    monkeypatch.delenv('PKA_ACTIVE_EXTENSIONS', raising=False)
    monkeypatch.setattr(extensions, '_loaded', None)
    extensions.activate()
    assert extensions.available('semantic')
    path = extensions.package_directory(root, item)
    (path / 'packages/example_capability.py').write_text('corrupted')
    extensions._leases.close()
    monkeypatch.delenv('PKA_ACTIVE_EXTENSIONS', raising=False)
    monkeypatch.setattr(extensions, '_loaded', None)
    extensions.activate()
    assert extensions.status()['semantic']['status'] == 'failed'


def test_legacy_full_runtime_retains_capabilities(tmp_path, monkeypatch):
    monkeypatch.setattr(extensions, 'runtime_root', lambda: tmp_path)
    monkeypatch.setattr(extensions, '_loaded', None)
    assert all(extensions.available(key) for key in extensions.IDS)
    assert installer.snapshot(tmp_path, tmp_path / 'state')['bundled']


def test_capability_status_requires_owner_and_cloud_ai_is_bundled(client, account, monkeypatch):
    monkeypatch.setattr(extensions, '_loaded', {key: {'status': 'not_installed'} for key in extensions.IDS})
    assert client.get('/v1/runtime-extensions').status_code == 401
    response = client.get('/v1/runtime-extensions', headers=account['headers'])
    assert response.json()['cloud_ai'] == 'bundled'
    assert response.json()['extensions']['ocr']['status'] == 'not_installed'


def test_missing_document_extension_waits_without_losing_original(client, account, tmp_path, monkeypatch):
    from app import files
    from services.worker import main as worker
    configuration = replace(files.settings, upload_dir=tmp_path / 'uploads')
    monkeypatch.setattr(files, 'settings', configuration)
    monkeypatch.setattr(worker, 'settings', configuration)
    monkeypatch.setattr(extensions, '_loaded', {key: {'status': 'not_installed'} for key in extensions.IDS})
    response = client.post('/v1/file-captures', params={'domain_id': account['domain']['id'], 'filename': 'test.docx'},
        content=b'original document', headers={**account['headers'], 'Idempotency-Key': 'waiting'})
    assert response.status_code == 201
    with SessionLocal() as db:
        assert worker.run_document_once(db)
        job = db.scalar(select(DocumentJob))
        assert job.status == 'pending' and extensions.job_status(job) == 'waiting_extension'
        assert db.get(Source, job.source_id).processing_status == 'pending'
        assert not worker.run_document_once(db)
    assert next(configuration.upload_dir.iterdir()).read_bytes() == b'original document'


def test_partial_pdf_text_is_saved_while_ocr_waits(client, account, tmp_path, monkeypatch):
    from app import files
    from services.worker import main as worker
    configuration = replace(files.settings, upload_dir=tmp_path / 'uploads')
    monkeypatch.setattr(files, 'settings', configuration)
    monkeypatch.setattr(worker, 'settings', configuration)
    monkeypatch.setattr(extensions, '_loaded', {key: {'status': 'not_installed'} for key in extensions.IDS})
    response = client.post('/v1/file-captures', params={'domain_id': account['domain']['id'], 'filename': 'mixed.pdf'},
        content=b'%PDF-test', headers={**account['headers'], 'Idempotency-Key': 'mixed'})
    assert response.status_code == 201
    def extract(*args):
        error = extensions.ExtensionRequired('ocr')
        error.partial_pages = [(1, '可读取的数字版正文'), (2, '')]
        raise error
    with SessionLocal() as db:
        assert worker.run_ocr_once(db, extractor=extract)
        job = db.scalar(select(OCRJob))
        source = db.get(Source, job.source_id)
        assert job.status == 'pending' and extensions.job_status(job) == 'waiting_extension'
        assert source.coverage == 'partial_text' and source.processing_status == 'usable'
        assert db.get(SourceRevision, source.current_revision_id).extracted_text == '可读取的数字版正文'


def test_semantic_without_extension_reports_missing_instead_of_keyword_fallback(client, account, monkeypatch):
    monkeypatch.setattr(extensions, '_loaded', {key: {'status': 'not_installed'} for key in extensions.IDS})
    result = client.post('/v1/search', headers=account['headers'], json={'query': 'test', 'mode': 'semantic'})
    assert result.status_code == 409
    assert '扩展' in result.text
    result = client.post('/v1/search', headers=account['headers'], json={'query': 'test', 'mode': 'exact'})
    assert result.status_code == 200


@pytest.mark.parametrize('pdf', [False, True])
def test_edit_while_waiting_cancels_auto_parse_and_explicit_retry_keeps_history(client, account, tmp_path, monkeypatch, pdf):
    from app import files, ocr
    from services.worker import main as worker
    configuration = replace(files.settings, upload_dir=tmp_path / 'uploads')
    for module in (files, ocr, worker):
        monkeypatch.setattr(module, 'settings', configuration)
    monkeypatch.setattr(extensions, '_loaded', {key: {'status': 'not_installed'} for key in extensions.IDS})
    response = client.post('/v1/file-captures', params={'domain_id': account['domain']['id'],
        'filename': 'waiting.pdf' if pdf else 'waiting.docx'}, content=b'%PDF-test' if pdf else b'original',
        headers={**account['headers'], 'Idempotency-Key': 'edit-waiting'})
    assert response.status_code == 201, response.text
    source_id = response.json()['id']
    run = worker.run_ocr_once if pdf else worker.run_document_once
    def missing(*args):
        raise extensions.ExtensionRequired('ocr' if pdf else 'documents')
    with SessionLocal() as db:
        assert run(db, extractor=missing)
    edited = client.post(f'/v1/sources/{source_id}/revisions', headers=account['headers'],
                         json={'text': '用户手动编辑的正文'}, follow_redirects=False)
    assert edited.status_code == 201, edited.text
    monkeypatch.setattr(extensions, '_loaded', {key: {'status': 'ready'} for key in extensions.IDS})
    with SessionLocal() as db:
        assert not run(db, extractor=lambda *args: pytest.fail('不应自动覆盖手动编辑'))
        job = db.scalar(select(OCRJob if pdf else DocumentJob))
        assert job.status == 'cancelled' and job.last_error == extensions.MANUAL_EDIT_PRESERVED
        source = db.get(Source, source_id)
        revision_id = source.current_revision_id
        assert source.processing_status == 'usable'
    path = 'ocr' if pdf else 'document-parse'
    assert client.post(f'/v1/sources/{source_id}/{path}', headers=account['headers']).status_code == 202
    with SessionLocal() as db:
        assert run(db, extractor=lambda *args: [(1, '重新扫描结果')] if pdf else '重新解析结果')
        assert db.get(SourceRevision, revision_id).extracted_text == '用户手动编辑的正文'
        source = db.get(Source, source_id)
        assert db.get(SourceRevision, source.current_revision_id).extracted_text.startswith('重新')


def test_advanced_pdf_keeps_digital_pages_when_ocr_extension_missing(tmp_path, monkeypatch):
    from services.worker.pdf_ocr import extract_pdf_inspector
    closed = []
    class PDF:
        def __len__(self): return 2
        def __getitem__(self, index): return SimpleNamespace(close=lambda: closed.append(index))
        def close(self): closed.append('pdf')
    pages = [SimpleNamespace(page=0, markdown='数字正文', needs_ocr=False),
             SimpleNamespace(page=1, markdown='已有布局文字', needs_ocr=True)]
    monkeypatch.setattr(extensions, '_loaded', {'documents': {'status': 'ready'}, 'ocr': {'status': 'not_installed'}})
    monkeypatch.setitem(sys.modules, 'pypdfium2', SimpleNamespace(PdfDocument=lambda _: PDF()))
    monkeypatch.setitem(sys.modules, 'pdf_inspector', SimpleNamespace(
        extract_pages_markdown=lambda _: SimpleNamespace(pages=pages)))
    with pytest.raises(extensions.ExtensionRequired) as error:
        extract_pdf_inspector(tmp_path / 'mixed.pdf', 'small')
    assert error.value.partial_pages == [(1, '数字正文'), (2, '已有布局文字')]
    assert closed == [1, 'pdf']
