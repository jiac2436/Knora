"""Portable exports retain knowledge while excluding runtime credentials."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import subprocess
import sys
import warnings
import zipfile

import pytest
from sqlalchemy.orm import Session

from app import auth, local_readable_export
from app.database import make_engine
from app.local_backup import verify_backup
from app.local_runtime import data_lock
from app.models import Domain, EvidenceChunk, FileAsset, Source, SourceImage, SourceRevision, User
from test_local_backup import library  # Explicitly reuse the isolated synthetic library fixture.


@pytest.fixture
def readable_library(library):
    paths, identity, payload = library
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        db.add(SourceRevision(id='historical-revision', source_id='source', version=6,
                              extracted_text='历史中文正文\n保留上一版本'))
        db.flush()
        db.add(EvidenceChunk(id='historical-evidence', revision_id='historical-revision',
                             ordinal=0, text='历史中文正文', locator_kind='page',
                             locator_start=2, locator_end=3))
        db.add(Source(id='deleted-source', owner_id=identity['owner_id'], domain_id='domain',
                      kind='note', title='已删除私有标题', raw_input='已删除私有正文',
                      coverage='full', deleted_at=datetime.now(timezone.utc)))
        db.flush()
        db.add(SourceRevision(id='deleted-revision', source_id='deleted-source', version=1,
                              extracted_text='已删除私有正文'))
    engine.dispose()
    return library


def read_export(package):
    with zipfile.ZipFile(package) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    return entries, json.loads(entries['manifest.json']), json.loads(entries['metadata.json'])


def test_readable_export_preserves_versions_evidence_context_and_originals(tmp_path, readable_library):
    paths, identity, payload = readable_library
    before = paths.database.read_bytes()
    package = tmp_path / 'readable.zip'
    result = local_readable_export.export_readable(paths, package)
    entries, manifest, metadata = read_export(package)
    assert result['format'] == manifest['format'] == metadata['format'] == 'pka-readable-export'
    assert manifest['version'] == metadata['version'] == 1
    assert len(metadata['sources']) == 1
    source = metadata['sources'][0]
    assert source['id'] == 'source'
    assert source['title'] == '往返测试'
    assert source['domain']['name'] == '合成资料'
    assert source['personal_context']['notes'] == '用户补充'
    assert source['current_revision_id'] == 'revision'
    revisions = {item['version']: item for item in source['revisions']}
    assert set(revisions) == {6, 7}
    assert revisions[6]['extracted_text'] == '历史中文正文\n保留上一版本'
    assert revisions[7]['extracted_text'] == '原始正文'
    evidence = revisions[6]['evidence'][0]
    assert (evidence['id'], evidence['text'], evidence['locator_kind'],
            evidence['locator_start'], evidence['locator_end']) == (
                'historical-evidence', '历史中文正文', 'page', 2, 3)
    assert '用户补充' in entries[source['path']].decode('utf-8')
    for revision in revisions.values():
        markdown = entries[revision['path']].decode('utf-8')
        assert all(line in markdown for line in revision['extracted_text'].splitlines())
    assert source['file_asset']['filename'] == 'sample.txt'
    assert PurePosixPath(source['file_asset']['path']).suffix == '.txt'
    assert entries[source['file_asset']['path']] == payload
    for item in manifest['files']:
        content = entries[item['path']]
        assert item['size_bytes'] == len(content)
        assert item['sha256'] == hashlib.sha256(content).hexdigest()
    assert set(entries) == {'manifest.json', *(item['path'] for item in manifest['files'])}
    assert paths.database.read_bytes() == before
    assert (paths.assets / 'asset-one').read_bytes() == payload


def test_export_excludes_deleted_content_credentials_and_runtime_state(tmp_path, readable_library):
    paths = readable_library[0]
    (paths.data / 'jobs' / 'queue-secret').write_text('client-queue-secret')
    (paths.assets / 'unreferenced-secret').write_text('unreferenced-file-secret')
    package = tmp_path / 'readable.zip'
    local_readable_export.export_readable(paths, package)
    entries, _, _ = read_export(package)
    all_bytes = b'\n'.join(entries.values())
    for secret in ('已删除私有标题', '已删除私有正文', 'private-password-hash',
                   'private-worker-error', 'client-queue-secret', 'unreferenced-file-secret',
                   auth.token_hash('a' * 48), '1' * 64, '2' * 64):
        assert secret.encode() not in all_bytes
    assert not any(name.endswith(('.db', '.sqlite', '.sqlite3')) for name in entries)
    assert not any('queue' in name or 'session' in name for name in entries)
    with pytest.raises(ValueError):
        verify_backup(package)


def test_deleted_missing_original_does_not_block_live_readable_export(tmp_path, readable_library):
    paths = readable_library[0]
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        db.add(FileAsset(id='missing-deleted-file', source_id='deleted-source', filename='deleted.txt',
                         size_bytes=10, sha256='4' * 64))
    engine.dispose()
    package = tmp_path / 'live-only.zip'
    local_readable_export.export_readable(paths, package)
    _, _, metadata = read_export(package)
    assert [source['id'] for source in metadata['sources']] == ['source']


@pytest.mark.parametrize('original_exists', [True, False])
def test_foreign_owner_content_and_originals_are_excluded(tmp_path, readable_library, original_exists):
    paths = readable_library[0]
    foreign_original = b'foreign-owner-private-original'
    if original_exists:
        (paths.assets / 'foreign-file').write_bytes(foreign_original)
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        db.add(User(id='foreign-owner', email='foreign@example.invalid', name='其他用户'))
        db.flush()
        db.add(Domain(id='foreign-domain', owner_id='foreign-owner', name='其他用户领域'))
        db.flush()
        db.add(Source(id='foreign-source', owner_id='foreign-owner', domain_id='foreign-domain',
                      kind='file', title='其他用户私有标题', raw_input='其他用户私有正文', coverage='full'))
        db.flush()
        db.add(SourceRevision(id='foreign-revision', source_id='foreign-source', version=1,
                              extracted_text='其他用户私有历史正文'))
        db.add(FileAsset(id='foreign-file', source_id='foreign-source', filename='foreign.txt',
                         size_bytes=len(foreign_original), sha256=hashlib.sha256(foreign_original).hexdigest()))
    engine.dispose()
    before = paths.database.read_bytes()
    package = tmp_path / 'owner-only.zip'
    local_readable_export.export_readable(paths, package)
    entries, _, metadata = read_export(package)
    assert [source['id'] for source in metadata['sources']] == ['source']
    all_bytes = b'\n'.join(entries.values())
    for secret in ('其他用户私有标题', '其他用户私有正文', '其他用户私有历史正文', '其他用户领域'):
        assert secret.encode() not in all_bytes
    assert foreign_original not in all_bytes
    assert paths.database.read_bytes() == before
    if original_exists:
        assert (paths.assets / 'foreign-file').read_bytes() == foreign_original


def test_untrusted_ids_titles_and_original_names_cannot_control_archive_paths(tmp_path, readable_library):
    paths, identity, payload = readable_library
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        db.get(FileAsset, 'asset-one').filename = '../../逃逸.txt'
        db.add(Source(id='../../escape-source', owner_id=identity['owner_id'], domain_id='domain',
                      kind='note', title='../逃逸/<script>alert(1)</script>', coverage='full'))
        db.flush()
        db.add(SourceRevision(id='../../escape-revision', source_id='../../escape-source',
                              version=1, extracted_text='<script>危险原文</script>\n[链接](file:///private/secret)'))
    engine.dispose()
    package = tmp_path / 'readable.zip'
    local_readable_export.export_readable(paths, package)
    entries, _, metadata = read_export(package)
    for name in entries:
        assert not PurePosixPath(name).is_absolute()
        assert '..' not in PurePosixPath(name).parts
        assert '\\' not in name
        assert 'escape-source' not in name and '逃逸' not in name
    untrusted = next(source for source in metadata['sources'] if source['id'] == '../../escape-source')
    assert untrusted['title'] == '../逃逸/<script>alert(1)</script>'
    revision = untrusted['revisions'][0]
    assert revision['id'] == '../../escape-revision'
    assert revision['extracted_text'] == '<script>危险原文</script>\n[链接](file:///private/secret)'
    markdown = entries[revision['path']].decode()
    assert all(not line.startswith(('<script>', '[链接]')) for line in markdown.splitlines())
    assert not (tmp_path / 'escape-source').exists()
    original = next(source for source in metadata['sources'] if source['id'] == 'source')
    assert original['file_asset']['filename'] == '../../逃逸.txt'
    assert entries[original['file_asset']['path']] == payload


@pytest.mark.parametrize('filename, expected_suffix', [
    ('中文报告.csv', '.csv'), ('../../中文原件.TXT', '.txt'), ('报告.pdf', '.pdf'),
    ('../../恶意.command', ''), ('网页.html', ''), ('伪装.txt.command', ''),
])
def test_original_extension_is_allowlisted_without_changing_original_name_or_bytes(
        tmp_path, readable_library, filename, expected_suffix):
    paths, _, payload = readable_library
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        db.get(FileAsset, 'asset-one').filename = filename
    engine.dispose()
    package = tmp_path / 'extensions.zip'
    local_readable_export.export_readable(paths, package)
    entries, _, metadata = read_export(package)
    asset = metadata['sources'][0]['file_asset']
    assert asset['filename'] == filename
    assert PurePosixPath(asset['path']).suffix == expected_suffix
    assert PurePosixPath(asset['path']).parent == PurePosixPath('assets')
    assert entries[asset['path']] == payload
    assert (paths.assets / 'asset-one').read_bytes() == payload


def test_saved_images_export_exact_bytes_and_missing_saved_images_fail(tmp_path, readable_library):
    paths = readable_library[0]
    image = paths.assets / 'images' / 'image-file'
    image.parent.mkdir()
    content = b'synthetic-image-bytes'
    diagnostic_secret = 'Bearer synthetic-private-image-download-token'
    image.write_bytes(content)
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        db.add(SourceImage(id='image', source_id='source', revision_id='revision', ordinal=0,
                           status='saved', caption='中文图片说明', ocr_text='图片识别正文',
                           file_key='image-file', size_bytes=len(content),
                           sha256=hashlib.sha256(content).hexdigest(), mime_type='image/png'))
        db.add(SourceImage(id='failed-image', source_id='source', revision_id='revision', ordinal=1,
                           status='failed', caption='失败图片说明', ocr_text='失败前保留识别知识',
                           failure_reason=diagnostic_secret))
    engine.dispose()
    package = tmp_path / 'images.zip'
    local_readable_export.export_readable(paths, package)
    entries, _, metadata = read_export(package)
    images = {item['id']: item for item in metadata['sources'][0]['images']}
    assert images['image']['caption'] == '中文图片说明'
    assert images['image']['ocr_text'] == '图片识别正文'
    assert PurePosixPath(images['image']['path']).suffix == '.png'
    assert entries[images['image']['path']] == content
    assert images['failed-image']['status'] == 'failed'
    assert images['failed-image']['caption'] == '失败图片说明'
    assert images['failed-image']['ocr_text'] == '失败前保留识别知识'
    assert images['failed-image'].get('path') is None
    assert diagnostic_secret.encode() not in b'\n'.join(entries.values())
    with sqlite3.connect(paths.database) as db:
        assert db.execute("SELECT failure_reason FROM source_images WHERE id='failed-image'").fetchone()[0] == diagnostic_secret
    image.unlink()
    with pytest.raises(ValueError):
        local_readable_export.export_readable(paths, tmp_path / 'missing-image.zip')
    assert not (tmp_path / 'missing-image.zip').exists()


@pytest.mark.parametrize('damage', ['missing', 'digest', 'symlink', 'unsafe-id'])
def test_invalid_original_refuses_publication(tmp_path, readable_library, damage):
    paths = readable_library[0]
    asset = paths.assets / 'asset-one'
    if damage == 'missing':
        asset.unlink()
    elif damage == 'digest':
        asset.write_bytes(b'damaged')
    elif damage == 'symlink':
        external = tmp_path / 'external'
        external.write_bytes(asset.read_bytes())
        asset.unlink()
        asset.symlink_to(external)
    else:
        with sqlite3.connect(paths.database) as db:
            db.execute("UPDATE file_assets SET id='../external' WHERE id='asset-one'")
    before = paths.database.read_bytes()
    package = tmp_path / 'readable.zip'
    with pytest.raises(ValueError):
        local_readable_export.export_readable(paths, package)
    assert not package.exists()
    assert paths.database.read_bytes() == before


def test_readable_export_requires_stopped_library_and_preserves_existing_output(tmp_path, readable_library):
    paths = readable_library[0]
    package = tmp_path / 'readable.zip'
    with data_lock(paths, exclusive=False):
        with pytest.raises(ValueError):
            local_readable_export.export_readable(paths, package)
    assert not package.exists()
    package.write_bytes(b'existing-file')
    with pytest.raises(ValueError):
        local_readable_export.export_readable(paths, package)
    assert package.read_bytes() == b'existing-file'
    with pytest.raises(ValueError):
        local_readable_export.export_readable(paths, paths.backups / 'unsafe.zip')


@pytest.mark.parametrize('limit', ['MAX_BYTES', 'MAX_MANIFEST'])
def test_size_limits_refuse_publication_without_touching_original(tmp_path, readable_library, monkeypatch, limit):
    paths = readable_library[0]
    before = paths.database.read_bytes()
    monkeypatch.setattr(local_readable_export, limit, 1)
    package = tmp_path / 'oversized.zip'
    with pytest.raises(ValueError):
        local_readable_export.export_readable(paths, package)
    assert not package.exists()
    assert paths.database.read_bytes() == before


@pytest.mark.parametrize('damage', ['digest', 'size', 'traversal', 'duplicate', 'unlisted', 'symlink'])
def test_readable_verifier_rejects_damaged_or_unsafe_packages(tmp_path, readable_library, damage):
    package = tmp_path / 'readable.zip'
    local_readable_export.export_readable(readable_library[0], package)
    entries, manifest, _ = read_export(package)
    name = 'index.md'
    if damage == 'digest':
        entries[name] = b'x' * len(entries[name])
    elif damage == 'size':
        entries[name] += b'extra'
    elif damage == 'traversal':
        entries['../escape.md'] = entries.pop(name)
        next(item for item in manifest['files'] if item['path'] == name)['path'] = '../escape.md'
        entries['manifest.json'] = json.dumps(manifest).encode()
    elif damage == 'unlisted':
        entries['extra.md'] = b'undeclared'
    damaged = tmp_path / 'damaged.zip'
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(damaged, 'w') as archive:
            for path, content in entries.items():
                if damage == 'symlink' and path == name:
                    info = zipfile.ZipInfo(path)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, content)
                else:
                    archive.writestr(path, content)
            if damage == 'duplicate':
                archive.writestr(name, entries[name])
    with pytest.raises(ValueError):
        local_readable_export.verify_readable(damaged)
    assert not (tmp_path.parent / 'escape.md').exists()


@pytest.mark.parametrize('suffix', ['.command', '.html'])
def test_readable_verifier_rejects_unsafe_asset_extensions(tmp_path, readable_library, suffix):
    package = tmp_path / 'readable.zip'
    local_readable_export.export_readable(readable_library[0], package)
    entries, manifest, metadata = read_export(package)
    original = metadata['sources'][0]['file_asset']['path']
    malicious = str(PurePosixPath(original).with_suffix(suffix))
    entries[malicious] = entries.pop(original)
    next(item for item in manifest['files'] if item['path'] == original)['path'] = malicious
    entries['manifest.json'] = json.dumps(manifest).encode()
    damaged = tmp_path / 'unsafe-extension.zip'
    with zipfile.ZipFile(damaged, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    with pytest.raises(ValueError):
        local_readable_export.verify_readable(damaged)


def test_readable_cli_returns_single_json_and_leaves_source_unchanged(tmp_path, readable_library):
    paths = readable_library[0]
    before = paths.database.read_bytes()
    package = tmp_path / 'cli.zip'
    script = Path(__file__).resolve().parents[1] / 'app' / 'local_backup.py'
    command = [sys.executable, '-I', '-B', str(script), '--action', 'readable',
               '--package', str(package), '--data-dir', str(paths.data),
               '--cache-dir', str(paths.cache), '--log-dir', str(paths.logs)]
    response = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert response.returncode == 0, response.stdout + response.stderr
    assert len(response.stdout.splitlines()) == 1
    assert json.loads(response.stdout)['format'] == 'pka-readable-export'
    assert response.stderr == ''
    assert paths.database.read_bytes() == before
    response = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert response.returncode == 1
    assert len(response.stdout.splitlines()) == 1
    assert isinstance(json.loads(response.stdout)['error'], str)
