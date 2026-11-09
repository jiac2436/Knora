"""Local backups preserve knowledge without transferring executable credentials."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import stat
import warnings
import zipfile

import pytest
from sqlalchemy.orm import Session

from app import auth, migrations
from app.database import make_engine
from app.local_backup import export_backup, restore_backup, verify_backup
from app.local_paths import local_paths
from app.local_runtime import data_lock, initialize_identity
from app.models import (
    AccountToken, AgentConnection, AuditEvent, ConnectionGrant, DeletionJob,
    DocumentJob, Domain, EvidenceChunk, FileAsset, IdempotencyRecord,
    OCRJob, PasswordCredential, PersonalContext, SemanticIndexJob, Source,
    SourceRevision, WebReadJob,
)


def paths_for(root):
    return local_paths(root / 'data', root / 'cache', root / 'logs')


@pytest.fixture
def library(tmp_path):
    paths = paths_for(tmp_path / 'original')
    paths.prepare()
    engine = make_engine(f'sqlite:///{paths.database}')
    migrations.migrate(engine, paths.backups)
    identity = initialize_identity(engine, 'a' * 48)
    payload = b'original document\n'
    (paths.assets / 'asset-one').write_bytes(payload)
    expiration = datetime.now(timezone.utc) + timedelta(days=1)
    owner = identity['owner_id']
    with Session(engine) as db, db.begin():
        db.add(Domain(id='domain', owner_id=owner, name='合成资料'))
        db.flush()
        db.add(Source(id='source', owner_id=owner, domain_id='domain', kind='file',
                      title='往返测试', coverage='full', upload_status='local_saved',
                      processing_status='processing', current_revision_id='revision'))
        db.flush()
        db.add(SourceRevision(id='revision', source_id='source', version=7,
                              extracted_text='原始正文'))
        db.flush()
        db.add(EvidenceChunk(id='evidence', revision_id='revision', ordinal=0,
                             text='原始正文', locator_end=4))
        db.add(FileAsset(id='asset-one', source_id='source', filename='sample.txt',
                         size_bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest()))
        db.add(PersonalContext(source_id='source', notes='用户补充'))
        db.add(PasswordCredential(user_id=owner, password_hash='private-password-hash'))
        db.add(AccountToken(id='account-secret', user_id=owner, purpose='reset',
                            token_hash='1' * 64, expires_at=expiration))
        db.add(AgentConnection(id='connection', owner_id=owner, client_name='test-agent',
                               token_hash='2' * 64, expires_at=expiration))
        db.flush()
        db.add(ConnectionGrant(connection_id='connection', domain_id='domain'))
        db.add(AuditEvent(id='audit', owner_id=owner, connection_id='connection',
                          tool='test', result='ok'))
        db.add(IdempotencyRecord(id='idempotency', owner_id=owner, client_request_id='request',
                                 payload_hash='3' * 64, source_id='source'))
        db.add(DeletionJob(id='delete', owner_id=owner, source_id='source'))
        db.add(SemanticIndexJob(source_id='source', revision_id='revision', status='running'))
        db.add(OCRJob(id='ocr', owner_id=owner, source_id='source', model='small',
                      status='running', last_error='private-worker-error'))
        db.add(DocumentJob(id='document', owner_id=owner, source_id='source',
                           status='pending', last_error='private-worker-error'))
        db.add(WebReadJob(id='web', owner_id=owner, source_id='source', status='running',
                          last_error='private-worker-error'))
    engine.dispose()
    return paths, identity, payload


def package_for(tmp_path, library):
    package = tmp_path / 'backup.pka'
    export_backup(library[0], package)
    return package


def read_package_database(package, destination):
    with zipfile.ZipFile(package) as archive:
        destination.write_bytes(archive.read('knowledge.db'))
    return sqlite3.connect(destination)


def test_round_trip_preserves_original_versions_and_changes_device(tmp_path, library):
    paths, original_identity, payload = library
    source_hash = hashlib.sha256(paths.database.read_bytes()).hexdigest()
    package = package_for(tmp_path, library)
    verify_backup(package)
    destination = paths_for(tmp_path / 'restored')
    identity = restore_backup(package, destination, 'b' * 48)
    assert identity['owner_id'] == original_identity['owner_id']
    assert identity['library_id'] == original_identity['library_id']
    assert identity['device_id'] != original_identity['device_id']
    assert (destination.assets / 'asset-one').read_bytes() == payload
    assert hashlib.sha256(paths.database.read_bytes()).hexdigest() == source_hash
    with sqlite3.connect(destination.database) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('SELECT id, version, extracted_text FROM source_revisions').fetchone() == ('revision', 7, '原始正文')
        assert db.execute('SELECT id, text FROM evidence_chunks').fetchone() == ('evidence', '原始正文')
        assert db.execute('SELECT notes FROM personal_context').fetchone()[0] == '用户补充'
        assert db.execute('SELECT token_hash FROM sessions').fetchall() == [(auth.token_hash('b' * 48),)]
        for table in ('account_tokens', 'password_credentials', 'agent_connections', 'connection_grants',
                      'audit_events', 'idempotency_records', 'deletion_jobs', 'semantic_index_jobs'):
            assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
        for table in ('ocr_jobs', 'document_jobs', 'web_read_jobs'):
            status, error = db.execute(f'SELECT status, last_error FROM {table}').fetchone()
            assert status == 'failed'
            assert error is None or error == '恢复后请手动重试'


def test_exported_database_has_no_credentials_or_executable_tasks(tmp_path, library):
    package = package_for(tmp_path, library)
    with read_package_database(package, tmp_path / 'inspection.db') as db:
        for table in ('sessions', 'account_tokens', 'password_credentials', 'agent_connections',
                      'connection_grants', 'audit_events', 'idempotency_records', 'deletion_jobs', 'semantic_index_jobs'):
            assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
        assert db.execute('SELECT session_id FROM local_installations').fetchone()[0] is None
    with zipfile.ZipFile(package) as archive:
        database_bytes = archive.read('knowledge.db')
        assert b'private-password-hash' not in database_bytes
        assert auth.token_hash('a' * 48).encode() not in database_bytes
        assert b'private-worker-error' not in database_bytes
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['format'] == 'pka-local-backup'
        assert manifest['version'] == 1
        assert manifest['missing_assets'] == []
        assert set(archive.namelist()) == {'manifest.json', 'knowledge.db', 'assets/asset-one'}
        for item in manifest['files']:
            content = archive.read(item['path'])
            assert item['size_bytes'] == len(content)
            assert item['sha256'] == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize('damage', ['missing', 'digest', 'symlink'])
def test_export_refuses_invalid_originals_without_publishing(tmp_path, library, damage):
    paths = library[0]
    asset = paths.assets / 'asset-one'
    if damage == 'missing':
        asset.unlink()
    elif damage == 'digest':
        asset.write_bytes(b'changed document\n')
    else:
        other = tmp_path / 'outside'
        other.write_bytes(asset.read_bytes())
        asset.unlink()
        asset.symlink_to(other)
    package = tmp_path / 'backup.pka'
    with pytest.raises(ValueError):
        export_backup(paths, package)
    assert not package.exists()


def test_running_library_rejects_export_and_restore(tmp_path, library):
    paths = library[0]
    package = package_for(tmp_path, library)
    with data_lock(paths, exclusive=False):
        with pytest.raises(ValueError):
            export_backup(paths, tmp_path / 'busy.pka')
    destination = paths_for(tmp_path / 'restored')
    with data_lock(destination, exclusive=False, prepare=False):
        with pytest.raises(ValueError):
            restore_backup(package, destination, 'b' * 48)
    assert not destination.data.exists()


def test_export_and_restore_never_overwrite(tmp_path, library):
    paths = library[0]
    package = package_for(tmp_path, library)
    original_package = package.read_bytes()
    with pytest.raises(ValueError):
        export_backup(paths, package)
    assert package.read_bytes() == original_package
    with pytest.raises(ValueError):
        export_backup(paths, paths.backups / 'invalid.pka')
    destination = paths_for(tmp_path / 'occupied')
    destination.data.mkdir(parents=True)
    marker = destination.data / 'keep'
    marker.write_text('preserve')
    with pytest.raises(ValueError):
        restore_backup(package, destination, 'b' * 48)
    assert marker.read_text() == 'preserve'


def test_uninitialized_library_cannot_be_exported(tmp_path):
    paths = paths_for(tmp_path / 'uninitialized')
    paths.prepare()
    engine = make_engine(f'sqlite:///{paths.database}')
    migrations.migrate(engine, paths.backups)
    engine.dispose()
    with pytest.raises(ValueError):
        export_backup(paths, tmp_path / 'backup.pka')


def rewrite_package(original, destination, change):
    with zipfile.ZipFile(original) as archive:
        entries = [(name, archive.read(name)) for name in archive.namelist()]
    changed = change(entries)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(destination, 'w') as archive:
            for name, content in changed:
                archive.writestr(name, content)


@pytest.mark.parametrize('damage', ['format', 'version', 'traversal', 'duplicate', 'unlisted',
                                    'digest', 'size', 'symlink', 'invalid-database'])
def test_invalid_packages_do_not_publish(tmp_path, library, damage):
    package = package_for(tmp_path, library)
    damaged = tmp_path / 'damaged.pka'

    def change(entries):
        if damage in ('format', 'version', 'digest', 'size'):
            manifest = json.loads(dict(entries)['manifest.json'])
            if damage == 'format':
                manifest['format'] = 'unknown'
            elif damage == 'version':
                manifest['version'] = 99
            elif damage == 'digest':
                manifest['files'][0]['sha256'] = '0' * 64
            else:
                manifest['files'][0]['size_bytes'] += 1
            return [(name, json.dumps(manifest).encode() if name == 'manifest.json' else value)
                    for name, value in entries]
        if damage == 'traversal':
            manifest = json.loads(dict(entries)['manifest.json'])
            content = b'outside'
            manifest['files'].append({'path': '../outside.txt', 'size_bytes': len(content),
                                      'sha256': hashlib.sha256(content).hexdigest()})
            return [(name, json.dumps(manifest).encode() if name == 'manifest.json' else value)
                    for name, value in entries] + [('../outside.txt', content)]
        if damage == 'duplicate':
            return entries + [entries[0]]
        if damage == 'unlisted':
            return entries + [('assets/unlisted', b'not listed')]
        if damage == 'symlink':
            link = zipfile.ZipInfo('assets/link')
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            return entries + [(link, b'../../outside')]
        manifest = json.loads(dict(entries)['manifest.json'])
        invalid = b'not a sqlite database'
        for item in manifest['files']:
            if item['path'] == 'knowledge.db':
                item['size_bytes'] = len(invalid)
                item['sha256'] = hashlib.sha256(invalid).hexdigest()
        return [(name, json.dumps(manifest).encode() if name == 'manifest.json'
                 else invalid if name == 'knowledge.db' else value) for name, value in entries]

    rewrite_package(package, damaged, change)
    destination = paths_for(tmp_path / 'restored')
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        restore_backup(damaged, destination, 'b' * 48)
    assert not destination.data.exists()
    assert not (tmp_path / 'outside.txt').exists()
    if damage != 'invalid-database':
        with pytest.raises(ValueError):
            verify_backup(damaged)


@pytest.mark.parametrize('limit', ['MAX_BYTES', 'MAX_MANIFEST'])
def test_package_resource_limits_block_export_and_restore(tmp_path, library, monkeypatch, limit):
    from app import local_backup
    package = package_for(tmp_path, library)
    monkeypatch.setattr(local_backup, limit, 1)
    with pytest.raises(ValueError):
        verify_backup(package)
    destination = paths_for(tmp_path / 'restored')
    with pytest.raises(ValueError):
        restore_backup(package, destination, 'b' * 48)
    assert not destination.data.exists()
    output = tmp_path / 'too-big.pka'
    with pytest.raises(ValueError):
        export_backup(library[0], output)
    assert not output.exists()


def rewrite_database_package(package, output, scratch, mutation):
    with zipfile.ZipFile(package) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    database = scratch / 'mutation.db'
    database.write_bytes(entries['knowledge.db'])
    with sqlite3.connect(database) as db:
        mutation(db)
    entries['knowledge.db'] = database.read_bytes()
    manifest = json.loads(entries['manifest.json'])
    for item in manifest['files']:
        if item['path'] == 'knowledge.db':
            item['size_bytes'] = len(entries['knowledge.db'])
            item['sha256'] = hashlib.sha256(entries['knowledge.db']).hexdigest()
    entries['manifest.json'] = json.dumps(manifest).encode()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


@pytest.mark.parametrize('damage', ['credential', 'active-task', 'active-web-image', 'unknown-table', 'foreign-key'])
def test_checksums_cannot_legitimize_unsafe_database(tmp_path, library, damage):
    package = package_for(tmp_path, library)
    forged = tmp_path / 'forged.pka'

    def mutation(db):
        if damage == 'credential':
            db.execute('INSERT INTO password_credentials (user_id,password_hash,failed_attempts) '
                       'SELECT owner_id,?,0 FROM local_installations', ('forged-secret',))
        elif damage == 'active-task':
            db.execute("UPDATE document_jobs SET status='pending'")
        elif damage == 'active-web-image':
            db.execute("INSERT INTO source_images (id,source_id,revision_id,ordinal,block_id,caption,status,"
                       "failure_reason,ocr_text,retrieval_method) "
                       "VALUES ('queued-image','source','revision',0,'','','pending','','','wechat_ua')")
        elif damage == 'unknown-table':
            db.execute('CREATE TABLE unexpected_records (secret TEXT)')
        else:
            db.execute("UPDATE file_assets SET source_id='missing-source'")

    rewrite_database_package(package, forged, tmp_path, mutation)
    with pytest.raises(ValueError):
        verify_backup(forged)
    destination = paths_for(tmp_path / 'restored')
    with pytest.raises(ValueError):
        restore_backup(forged, destination, 'b' * 48)
    assert not destination.data.exists()


@pytest.mark.parametrize('damage', ['extra-listed-asset', 'duplicate-manifest-record'])
def test_manifest_cannot_add_unreferenced_assets_or_duplicate_records(tmp_path, library, damage):
    package = package_for(tmp_path, library)
    forged = tmp_path / 'forged.pka'

    def change(entries):
        manifest = json.loads(dict(entries)['manifest.json'])
        addition = []
        if damage == 'extra-listed-asset':
            content = b'unreferenced original'
            manifest['files'].append({'path': 'assets/extra', 'size_bytes': len(content),
                                      'sha256': hashlib.sha256(content).hexdigest()})
            addition.append(('assets/extra', content))
        else:
            manifest['files'].append(dict(manifest['files'][0]))
        return [(name, json.dumps(manifest).encode() if name == 'manifest.json' else value)
                for name, value in entries] + addition

    rewrite_package(package, forged, change)
    with pytest.raises(ValueError):
        verify_backup(forged)
    destination = paths_for(tmp_path / 'restored')
    with pytest.raises(ValueError):
        restore_backup(forged, destination, 'b' * 48)
    assert not destination.data.exists()


def test_destination_created_during_restore_is_never_replaced(tmp_path, library, monkeypatch):
    from app import local_backup
    package = package_for(tmp_path, library)
    destination = paths_for(tmp_path / 'restored')
    publish = local_backup.publish_directory
    published_to = []

    def concurrent_create(stage, target):
        target.mkdir()
        published_to.append(target.stat().st_ino)
        publish(stage, target)

    monkeypatch.setattr(local_backup, 'publish_directory', concurrent_create)
    with pytest.raises(FileExistsError):
        restore_backup(package, destination, 'b' * 48)
    assert destination.data.stat().st_ino == published_to[0]
    assert list(destination.data.iterdir()) == []
    assert not destination.database.exists()


def test_valid_foreign_keys_cannot_bind_library_to_another_owner(tmp_path, library):
    package = package_for(tmp_path, library)
    forged = tmp_path / 'cross-owner.pka'

    def mutation(db):
        db.execute("INSERT INTO users (id,email,name,created_at) VALUES "
                   "('other-owner','other@example.invalid','另一个合成用户',CURRENT_TIMESTAMP)")
        db.execute("UPDATE libraries SET owner_id='other-owner'")
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []

    rewrite_database_package(package, forged, tmp_path, mutation)
    with pytest.raises(ValueError):
        verify_backup(forged)
    destination = paths_for(tmp_path / 'restored')
    with pytest.raises(ValueError):
        restore_backup(forged, destination, 'b' * 48)
    assert not destination.data.exists()
