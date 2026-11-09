"""Synthetic v1/v2 recovery packages never carry a former device's sync state."""
from contextlib import closing
import hashlib
import json
import sqlite3
import zipfile

import pytest
from sqlalchemy.orm import Session

from app.database import Base, make_engine
from app.local_backup import SYNC_TABLES, export_backup, restore_backup, verify_backup
from app.local_paths import local_paths
from app.local_runtime import initialize_identity
from app.migrations import migrate
from app.models import Domain, Source, SyncOperation


def paths_for(root):
    return local_paths(root / 'data', root / 'cache', root / 'logs')


@pytest.fixture
def library(tmp_path):
    paths = paths_for(tmp_path / 'original')
    paths.prepare()
    engine = make_engine(f'sqlite:///{paths.database}')
    migrate(engine, paths.backups)
    identity = initialize_identity(engine, 'a' * 48)
    with Session(engine) as db, db.begin():
        db.add(Domain(id='domain', owner_id=identity['owner_id'], name='合成领域'))
        db.flush()
        db.add(Source(id='source', owner_id=identity['owner_id'], domain_id='domain',
                      kind='note', title='恢复保留的资料', coverage='full'))
        db.flush()
        db.add(SyncOperation(id='former-operation', library_id=identity['library_id'],
                             device_id=identity['device_id'], entity_kind='source',
                             entity_id='source', generation=1, base_version=9,
                             payload_json='{"former_cloud_binding":"private-cloud-marker"}',
                             payload_sha256='a' * 64))
    with closing(sqlite3.connect(paths.database)) as db:
        db.execute("UPDATE replica_state SET epoch='private-cloud-marker',applied_sequence=73")
        db.execute('UPDATE sync_entities SET cloud_version=9')
        db.commit()
    engine.dispose()
    return paths, identity


def rewrite_database(original, destination, inspection, mutate):
    with zipfile.ZipFile(original) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    inspection.write_bytes(entries['knowledge.db'])
    with closing(sqlite3.connect(inspection)) as db:
        mutate(db)
        db.commit()
        db.execute('VACUUM')
    entries['knowledge.db'] = inspection.read_bytes()
    manifest = json.loads(entries['manifest.json'])
    for item in manifest['files']:
        if item['path'] == 'knowledge.db':
            item['size_bytes'] = len(entries['knowledge.db'])
            item['sha256'] = hashlib.sha256(entries['knowledge.db']).hexdigest()
    entries['manifest.json'] = json.dumps(manifest).encode()
    with zipfile.ZipFile(destination, 'w') as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def downgrade_v1(db):
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
        db.execute(f'DROP TRIGGER "{name}"')
    for table in SYNC_TABLES:
        db.execute(f'DROP TABLE {table}')
    for table in ('rag_jobs', 'rag_turns', 'rag_sessions', 'summary_search_terms', 'summary_embeddings', 'summary_index_jobs', 'ai_job_steps', 'ai_jobs', 'ai_artifacts'):
        db.execute(f'DROP TABLE {table}')
    for table in ('tag_suggestions', 'tag_rejections', 'tag_assignments', 'knowledge_tag_aliases', 'knowledge_tags'):
        db.execute(f'DROP TABLE {table}')
    db.execute('DELETE FROM schema_migrations WHERE version>=2')


def assert_new_replica(path, identity, previous):
    with closing(sqlite3.connect(path)) as db:
        replica = db.execute('SELECT library_id,device_id,epoch,applied_sequence FROM replica_state').fetchone()
        assert replica == (identity['library_id'], identity['device_id'], None, 0)
        assert identity['library_id'] == previous['library_id']
        assert identity['device_id'] != previous['device_id']
        assert db.execute('SELECT COUNT(*) FROM sync_operations').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM sync_entities WHERE cloud_version<>0 OR acknowledged_generation<>0').fetchone()[0] == 0
        assert db.execute('SELECT DISTINCT device_id FROM local_changes').fetchall() == [(identity['device_id'],)]
        assert db.execute('SELECT MIN(sequence) FROM local_changes').fetchone()[0] == 1
        assert db.execute("SELECT 1 FROM sync_entities WHERE entity_kind='source' AND entity_id='source'").fetchone()
        assert db.execute("SELECT title FROM sources WHERE id='source'").fetchone()[0] == '恢复保留的资料'


def test_v2_export_drops_sync_state_restore_builds_new_baseline(tmp_path, library):
    paths, previous = library
    package = tmp_path / 'v2.pka'
    export_backup(paths, package)
    verify_backup(package)
    with zipfile.ZipFile(package) as archive:
        content = archive.read('knowledge.db')
        assert b'private-cloud-marker' not in content
        assert b'former-operation' not in content
        inspection = tmp_path / 'inspection.db'
        inspection.write_bytes(content)
    with closing(sqlite3.connect(inspection)) as db:
        for table in SYNC_TABLES:
            assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
        assert db.execute("SELECT 1 FROM sqlite_sequence WHERE name='local_changes'").fetchone() is None
        from app.migrations import CURRENT_VERSION
        assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == CURRENT_VERSION
    destination = paths_for(tmp_path / 'restored')
    identity = restore_backup(package, destination, 'b' * 48)
    assert_new_replica(destination.database, identity, previous)
    with closing(sqlite3.connect(paths.database)) as db:
        assert db.execute('SELECT epoch,applied_sequence FROM replica_state').fetchone() == ('private-cloud-marker', 73)
        assert db.execute('SELECT id FROM sync_operations').fetchone()[0] == 'former-operation'


def test_real_v1_schema_package_restores_and_migrates(tmp_path, library):
    paths, previous = library
    with closing(sqlite3.connect(paths.database)) as db:
        downgrade_v1(db)
        db.commit()
    package = tmp_path / 'v1.pka'
    export_backup(paths, package)
    verify_backup(package)
    destination = paths_for(tmp_path / 'restored')
    identity = restore_backup(package, destination, 'b' * 48)
    assert_new_replica(destination.database, identity, previous)


@pytest.mark.parametrize('table', (*SYNC_TABLES, 'sqlite_sequence'))
def test_packages_with_former_replica_rows_are_rejected(tmp_path, library, table):
    paths, _ = library
    package = tmp_path / 'clean.pka'
    export_backup(paths, package)
    forged = tmp_path / 'forged.pka'

    def contaminate(db):
        with closing(sqlite3.connect(paths.database)) as original:
            query = "SELECT * FROM sqlite_sequence WHERE name='local_changes'" if table == 'sqlite_sequence' else f'SELECT * FROM {table} LIMIT 1'
            row = original.execute(query).fetchone()
        assert row is not None
        db.execute(f'INSERT INTO {table} VALUES ({",".join("?" for _ in row)})', row)

    rewrite_database(package, forged, tmp_path / 'inspection.db', contaminate)
    destination = paths_for(tmp_path / 'refused')
    with pytest.raises(ValueError):
        restore_backup(forged, destination, 'b' * 48)
    assert not destination.data.exists()


@pytest.mark.parametrize('damage', ['extra', 'missing', 'changed', 'v1-extra'])
def test_only_exact_v2_triggers_are_allowed(tmp_path, library, damage):
    package = tmp_path / 'clean.pka'
    export_backup(library[0], package)

    def corrupt(db):
        if damage == 'v1-extra':
            downgrade_v1(db)
        if damage in ('extra', 'v1-extra'):
            db.execute('CREATE TRIGGER injected AFTER UPDATE ON sources BEGIN SELECT 1; END')
        else:
            name = db.execute("SELECT name FROM sqlite_master WHERE type='trigger' LIMIT 1").fetchone()[0]
            db.execute(f'DROP TRIGGER "{name}"')
            if damage == 'changed':
                db.execute(f'CREATE TRIGGER "{name}" AFTER UPDATE ON sources BEGIN SELECT 1; END')

    damaged = tmp_path / 'damaged.pka'
    rewrite_database(package, damaged, tmp_path / 'inspection.db', corrupt)
    with pytest.raises(ValueError):
        verify_backup(damaged)


def test_development_create_all_does_not_enable_local_journal(tmp_path):
    engine = make_engine(f'sqlite:///{tmp_path / "development.db"}')
    try:
        Base.metadata.create_all(engine)
        initialize_identity(engine, 'a' * 48)
        with engine.connect() as connection:
            assert connection.exec_driver_sql('SELECT COUNT(*) FROM replica_state').scalar() == 0
    finally:
        engine.dispose()


def test_identity_and_journal_initialization_are_atomic(tmp_path, library):
    paths, _ = library
    with closing(sqlite3.connect(paths.database)) as db:
        previous = db.execute('SELECT session_id FROM local_installations').fetchone()[0]
        count = db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]
        db.execute("UPDATE replica_state SET device_id='another-device'")
        db.commit()
    engine = make_engine(f'sqlite:///{paths.database}')
    try:
        with pytest.raises(ValueError):
            initialize_identity(engine, 'b' * 48)
    finally:
        engine.dispose()
    with closing(sqlite3.connect(paths.database)) as db:
        assert db.execute('SELECT session_id FROM local_installations').fetchone()[0] == previous
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == count
