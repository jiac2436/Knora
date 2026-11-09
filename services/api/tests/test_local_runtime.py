from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import auth, migrations
from app.config import DEFAULT_DATABASE_URL, Settings
from app.database import make_engine
from app.errors import ApiError
from app.local_paths import local_paths
from app.local_runtime import data_lock, initialize_identity
from app.models import Session as UserSession, User


def paths_for(root):
    return local_paths(root / 'data', root / 'cache', root / 'logs')


def prepared_engine(root):
    paths = paths_for(root)
    paths.prepare()
    engine = make_engine(f'sqlite:///{paths.database}')
    migrations.migrate(engine, paths.backups)
    return paths, engine


def test_local_configuration(tmp_path):
    config = Settings(environment='local', database_url=DEFAULT_DATABASE_URL,
                      upload_dir=Path(__file__).resolve().parents[1] / 'uploads',
                      local_data_dir=str(tmp_path / 'data'), local_cache_dir=str(tmp_path / 'cache'),
                      local_log_dir=str(tmp_path / 'logs'))
    assert config.database_url == f'sqlite:///{tmp_path}/data/knowledge.db'
    assert config.upload_dir == tmp_path / 'data/assets'
    with pytest.raises(ValueError):
        replace(config, database_url='sqlite:////tmp/other.db')
    with pytest.raises(ValueError):
        local_paths('relative')
    with pytest.raises(ValueError):
        local_paths(tmp_path / 'Client.app/data')


def test_lock_shared_services_and_exclusive_preparation(tmp_path):
    paths = paths_for(tmp_path)
    with data_lock(paths, exclusive=False):
        with data_lock(paths, exclusive=False):
            with pytest.raises(ValueError):
                with data_lock(paths, exclusive=True):
                    pytest.fail('迁移锁不应成功')
    with data_lock(paths, exclusive=True):
        with pytest.raises(ValueError):
            with data_lock(paths, exclusive=False):
                pytest.fail('服务锁不应成功')
    with data_lock(paths, exclusive=True):
        pass


def test_identity_persistence_rotation_and_auth(tmp_path, monkeypatch):
    paths, engine = prepared_engine(tmp_path)
    try:
        identity = initialize_identity(engine, 'a' * 48)
        assert initialize_identity(engine, 'a' * 48) == identity
        assert initialize_identity(engine, 'b' * 48) == identity
        monkeypatch.setattr(auth, 'settings', replace(auth.settings, environment='local', database_url=DEFAULT_DATABASE_URL,
                          upload_dir=Path(__file__).resolve().parents[1] / 'uploads',
                          local_data_dir=str(paths.data), local_cache_dir=str(paths.cache), local_log_dir=str(paths.logs)))
        with Session(engine) as db:
            assert auth.require_app_principal('Bearer ' + 'b' * 48, db).owner_id == identity['owner_id']
            with pytest.raises(ApiError):
                auth.require_app_principal('Bearer ' + 'a' * 48, db)
            db.add(UserSession(user_id=identity['owner_id'], token_hash=auth.token_hash('c' * 48),
                               expires_at=datetime.now(timezone.utc) + timedelta(days=1)))
            db.commit()
            with pytest.raises(ApiError):
                auth.require_app_principal('Bearer ' + 'c' * 48, db)
        with pytest.raises(ValueError):
            initialize_identity(engine, 'd' * 48, 'another-user')
    finally:
        engine.dispose()


def test_existing_user_requires_explicit_selection(tmp_path):
    _, engine = prepared_engine(tmp_path)
    try:
        with Session(engine) as db:
            db.add(User(id='existing', email='existing@example.invalid', name='既有用户'))
            db.commit()
        with pytest.raises(ValueError):
            initialize_identity(engine, 'a' * 48)
        assert initialize_identity(engine, 'a' * 48, 'existing')['owner_id'] == 'existing'
    finally:
        engine.dispose()


def test_migration_failure_rolls_back_and_keeps_backup(tmp_path, monkeypatch):
    paths = paths_for(tmp_path)
    paths.prepare()
    with sqlite3.connect(paths.database) as db:
        db.execute('CREATE TABLE preserved (value TEXT)')
        db.execute("INSERT INTO preserved VALUES ('original')")
    engine = make_engine(f'sqlite:///{paths.database}')
    def failure(connection):
        connection.execute(text('CREATE TABLE partial (id INTEGER)'))
        raise RuntimeError('模拟迁移失败')
    monkeypatch.setattr(migrations, 'MIGRATIONS', {1: ('failed', failure)})
    try:
        with pytest.raises(RuntimeError):
            migrations.migrate(engine, paths.backups)
        for database in [paths.database, *paths.backups.glob('*.sqlite')]:
            with sqlite3.connect(database) as db:
                assert db.execute('SELECT value FROM preserved').fetchone()[0] == 'original'
                assert db.execute("SELECT name FROM sqlite_master WHERE name IN ('partial','schema_migrations')").fetchall() == []
        assert len(list(paths.backups.glob('*.sqlite'))) == 1
    finally:
        engine.dispose()


def test_legacy_migration_idempotent_and_unknown_version_refused(tmp_path):
    paths, engine = prepared_engine(tmp_path)
    try:
        with engine.begin() as connection:
            # Model a genuine pre-journal library, not a v2 database with erased metadata.
            from app.local_changes import EXPECTED_TRIGGER_SQL, SYNC_TABLES
            for name in EXPECTED_TRIGGER_SQL:
                connection.exec_driver_sql(f'DROP TRIGGER {name}')
            for table in SYNC_TABLES:
                connection.exec_driver_sql(f'DROP TABLE {table}')
            connection.execute(text('DROP TABLE schema_migrations'))
            connection.execute(text('ALTER TABLE ocr_jobs DROP COLUMN engine'))
        assert migrations.migrate(engine, paths.backups) is not None
        assert migrations.migrate(engine, paths.backups) is None
        with engine.begin() as connection:
            assert 'engine' in {row[1] for row in connection.exec_driver_sql('PRAGMA table_info(ocr_jobs)')}
            connection.execute(text("UPDATE schema_migrations SET version = 99 WHERE version = (SELECT MAX(version) FROM schema_migrations)"))
        with pytest.raises(ValueError):
            migrations.migrate(engine, paths.backups)
        assert len(list(paths.backups.glob('*.sqlite'))) == 1
    finally:
        engine.dispose()


def test_snapshot_includes_committed_wal(tmp_path):
    source = tmp_path / 'source.db'
    connection = sqlite3.connect(source)
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE items (value TEXT)')
        connection.execute("INSERT INTO items VALUES ('committed')")
        connection.commit()
        migrations.snapshot(source, tmp_path / 'backup.db')
        with sqlite3.connect(tmp_path / 'backup.db') as backup:
            assert backup.execute('SELECT value FROM items').fetchone()[0] == 'committed'
    finally:
        connection.close()


def test_prepare_and_local_api_in_separate_process(tmp_path):
    environment = {key: value for key, value in os.environ.items() if not key.startswith('PKA_')}
    environment.update(PKA_ENVIRONMENT='local', PKA_DATA_DIR=str(tmp_path / 'data'),
                       PKA_CACHE_DIR=str(tmp_path / 'cache'), PKA_LOG_DIR=str(tmp_path / 'logs'),
                       PKA_SEMANTIC_ENABLED='0', PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    command = [sys.executable, '-m', 'app.local_runtime', 'prepare', '--credential-stdin']
    result = subprocess.run(command, input='a' * 48 + '\n', text=True, capture_output=True, env=environment, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'a' * 48 not in result.stdout + result.stderr
    second = subprocess.run(command, input='a' * 48 + '\n', text=True, capture_output=True, env=environment, timeout=30)
    assert second.returncode == 0, second.stderr
    assert second.stdout == result.stdout
    script = '''
from fastapi.testclient import TestClient
from app.main import app
from app.local_runtime import data_lock
from app.config import settings
from app.database import SessionLocal
from app.models import Source
import subprocess, sys
with TestClient(app) as client:
    assert client.get('/v1/me', headers={'Authorization': 'Bearer ' + 'a' * 48}).status_code == 200
    assert client.post('/v1/dev/sessions', json={}).status_code == 404
    assert client.post('/v1/auth/register', json={}).status_code == 404
    headers = {'Authorization': 'Bearer ' + 'a' * 48, 'Idempotency-Key': 'local-test-capture'}
    domain = client.post('/v1/domains', headers=headers, json={'name': '本地测试'}).json()
    saved = client.post('/v1/captures', headers=headers, json={
        'capture_type': 'browser_selection', 'title': '本地保存',
        'content': {'text': '离线资料', 'image_urls': []}, 'coverage': 'partial_text',
        'context': {'domain_id': domain['id']}, 'client': {'kind': 'desktop', 'version': 'test'}})
    assert saved.status_code == 201, saved.text
    with SessionLocal() as db:
        assert db.query(Source).one().upload_status == 'local_saved'
    worker = subprocess.run([sys.executable, '-m', 'services.worker.main', '--once'],
                            capture_output=True, text=True, timeout=30)
    assert worker.returncode == 0, worker.stderr
    try:
        with data_lock(settings.local_paths, exclusive=True):
            raise AssertionError('运行时不允许迁移')
    except ValueError:
        pass
'''
    checked = subprocess.run([sys.executable, '-c', script], text=True, capture_output=True, env=environment, timeout=30)
    assert checked.returncode == 0, checked.stderr


def legacy_fixture(root):
    from app.database import Base
    from app.models import Domain, EvidenceChunk, FileAsset, Source, SourceRevision
    from app.local_import import file_digest
    root.mkdir()
    assets = root / 'uploads'
    assets.mkdir()
    (assets / 'asset-one').write_bytes(b'original file')
    engine = make_engine(f'sqlite:///{root / "old.db"}')
    Base.metadata.create_all(engine)
    with Session(engine) as db, db.begin():
        db.add(User(id='owner', email='owner@example.invalid', name='原用户'))
        db.flush()
        db.add(Domain(id='domain', owner_id='owner', name='测试领域'))
        db.flush()
        db.add(Source(id='source', owner_id='owner', domain_id='domain', kind='file', title='原文件', coverage='full'))
        db.flush()
        db.add(SourceRevision(id='revision', source_id='source', version=7, extracted_text='original'))
        db.flush()
        db.add(EvidenceChunk(id='evidence', revision_id='revision', ordinal=0, text='original', locator_end=8))
        db.add(FileAsset(id='asset-one', source_id='source', filename='test.txt', size_bytes=13,
                         sha256=file_digest(assets / 'asset-one')))
        db.add(UserSession(id='old-session', user_id='owner', token_hash=auth.token_hash('old' * 16),
                           expires_at=datetime.now(timezone.utc) + timedelta(days=1)))
    engine.dispose()
    return root / 'old.db', assets


def test_legacy_copy_preserves_source_and_ids(tmp_path):
    from app.local_import import file_digest, import_legacy
    from app.models import EvidenceChunk, Source, SourceRevision
    source, assets = legacy_fixture(tmp_path / 'legacy')
    old_digest = file_digest(source)
    destination = paths_for(tmp_path / 'new')
    identity = import_legacy(source, assets, destination, 'a' * 48, 'owner', source_stopped=True)
    assert identity['owner_id'] == 'owner'
    assert file_digest(source) == old_digest
    assert file_digest(destination.assets / 'asset-one') == file_digest(assets / 'asset-one')
    engine = make_engine(f'sqlite:///{destination.database}')
    try:
        with Session(engine) as db:
            assert db.get(SourceRevision, 'revision').version == 7
            assert db.get(EvidenceChunk, 'evidence').text == 'original'
            assert db.get(Source, 'source').upload_status == 'local_saved'
            assert db.get(UserSession, 'old-session').revoked_at is not None
        with pytest.raises(ValueError):
            import_legacy(source, assets, destination, 'b' * 48, 'owner', source_stopped=True)
    finally:
        engine.dispose()


@pytest.mark.parametrize('failure', ['active', 'wrong-owner', 'bad-asset', 'newer-version'])
def test_legacy_failure_does_not_publish(tmp_path, failure):
    from app.local_import import file_digest, import_legacy
    source, assets = legacy_fixture(tmp_path / 'legacy')
    if failure == 'bad-asset':
        (assets / 'asset-one').write_bytes(b'damaged')
    if failure == 'newer-version':
        with sqlite3.connect(source) as db:
            db.execute('CREATE TABLE schema_migrations (version INTEGER, name TEXT)')
            db.execute("INSERT INTO schema_migrations VALUES (99, 'future')")
    old_digest = file_digest(source)
    destination = paths_for(tmp_path / 'new')
    with pytest.raises(ValueError):
        import_legacy(source, assets, destination, 'a' * 48,
                      'missing' if failure == 'wrong-owner' else 'owner', source_stopped=failure != 'active')
    assert not destination.data.exists()
    assert file_digest(source) == old_digest
