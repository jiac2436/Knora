"""Normalized local cache preserves retrieval, migration, and backup contracts."""
import sqlite3
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import migrations, services, local_backup
from app.database import make_engine
from app.local_backup import export_backup, restore_backup
from app.local_changes import EXPECTED_TRIGGER_SQL, install_change_triggers, validate_change_triggers
from app.local_paths import local_paths
from app.models import Source, SourceSearchDocument, SourceSearchTerm
from app.schemas import SearchRequest
from test_local_changes import local_library, capture_note


def legacy_cache(connection, version=13):
    rows = connection.exec_driver_sql('''SELECT d.source_id,t.term,d.owner_id FROM source_search_terms t
        JOIN source_search_documents d ON d.id=t.document_id''').all()
    connection.exec_driver_sql('DROP TABLE source_search_terms')
    connection.exec_driver_sql('DROP TABLE source_search_documents')
    connection.exec_driver_sql('''CREATE TABLE source_search_terms (source_id VARCHAR(36) NOT NULL REFERENCES sources(id),
        term VARCHAR(128) NOT NULL, owner_id VARCHAR(36) NOT NULL, PRIMARY KEY(source_id,term))''')
    connection.exec_driver_sql('CREATE INDEX ix_source_search_terms_owner_term ON source_search_terms(owner_id,term)')
    if rows:
        connection.exec_driver_sql('INSERT INTO source_search_terms VALUES (?,?,?)', [tuple(row) for row in rows])
    for name in EXPECTED_TRIGGER_SQL:
        connection.exec_driver_sql(f'DROP TRIGGER IF EXISTS "{name}"')
    additions = ((15, migrations.DOMAIN_FULL_ANALYSIS_TABLES), (13, migrations.DOMAIN_NODE_INDEX_TABLES), (12, migrations.DOMAIN_BUILD_TABLES),
                 (11, migrations.DOMAIN_SUMMARY_TABLES), (10, migrations.DOMAIN_TOPIC_TABLES),
                 (9, migrations.DOMAIN_PROCESSING_TABLES))
    for introduced, tables in additions:
        if version < introduced:
            for name in sorted(tables, reverse=True):
                connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{name}"')
    if version < 12:
        connection.exec_driver_sql('DROP INDEX IF EXISTS ix_node_step_key')
    connection.exec_driver_sql('DELETE FROM schema_migrations WHERE version>?', (version,))
    install_change_triggers(connection, version=version)
    migrations.require_schema(connection, allow_previous=True)
    return rows


def cache_rows(connection):
    return connection.exec_driver_sql('''SELECT d.source_id,t.term,d.owner_id FROM source_search_terms t
        JOIN source_search_documents d ON d.id=t.document_id ORDER BY d.source_id,t.term''').all()


def test_legacy_v13_migration_preserves_every_token_and_backup(local_library):
    paths, engine, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    with sessions() as db:
        source = db.get(Source, saved['id']); source.title = 'StreamingSSR 知识关联 abcdef'
        services.reindex_source(db, source); db.commit()
    with engine.begin() as connection:
        expected = sorted(legacy_cache(connection))
        source_before = connection.exec_driver_sql('SELECT id,owner_id,current_revision_id,title FROM sources').all()
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
    backup = migrations.migrate(engine, paths.backups)
    assert backup is not None
    with sqlite3.connect(backup) as db:
        assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 13
        assert db.execute('SELECT COUNT(*) FROM source_search_terms').fetchone()[0] == len(expected)
    with engine.connect() as connection:
        assert cache_rows(connection) == expected
        assert connection.exec_driver_sql('SELECT id,owner_id,current_revision_id,title FROM sources').all() == source_before
        assert migrations.schema_version(connection) == migrations.CURRENT_VERSION
        assert 'WITHOUT ROWID' in connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='source_search_terms'").scalar()
        assert connection.exec_driver_sql('PRAGMA integrity_check').scalar() == 'ok'
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').first() is None
        validate_change_triggers(connection, version=migrations.CURRENT_VERSION)
    assert migrations.migrate(engine, paths.backups) is None
    with sessions() as db:
        for query in ('stream', 'abcd', '知识', '合成补充'):
            assert services.search_knowledge(db, identity['owner_id'], SearchRequest(query=query))[0]['source_id'] == saved['id']


def test_wrong_cached_owner_is_discarded_and_rebuilt_without_leaking(local_library):
    paths, engine, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    wrong_owner = str(uuid4())
    with engine.begin() as connection:
        legacy_cache(connection)
        connection.exec_driver_sql('UPDATE source_search_terms SET owner_id=? WHERE source_id=?', (wrong_owner, saved['id']))
    migrations.migrate(engine, paths.backups)
    with sessions() as db:
        assert db.scalar(select(SourceSearchDocument)) is None
        assert services.search_knowledge(db, wrong_owner, SearchRequest(query='合成')) == []
        services.ensure_search_index(db)
        document = db.scalar(select(SourceSearchDocument))
        assert document.owner_id == identity['owner_id']
        assert services.search_knowledge(db, identity['owner_id'], SearchRequest(query='合成'))[0]['source_id'] == saved['id']
        # Even a corrupted current cache cannot grant access to another Owner.
        document.owner_id = wrong_owner; db.commit()
        assert services.search_knowledge(db, wrong_owner, SearchRequest(query='合成')) == []
        services.ensure_search_index(db)
        assert db.get(SourceSearchDocument, document.id).owner_id == identity['owner_id']


@pytest.mark.parametrize('version', range(8, 14))
def test_old_backup_restore_rebuilds_compact_cache(local_library, tmp_path, monkeypatch, version):
    paths, engine, _, identity, _ = local_library
    saved = capture_note(local_library)
    with engine.begin() as connection:
        legacy_cache(connection, version)
        if version == 13:
            connection.exec_driver_sql('UPDATE source_search_terms SET owner_id=?', (str(uuid4()),))
    package = tmp_path / f'old-v{version}.pka'
    # Emulate an actual old exporter: v8-v13 backups retained keyword rows.
    with monkeypatch.context() as patch:
        patch.setattr(local_backup, 'REMOVED_TABLES', tuple(name for name in local_backup.REMOVED_TABLES
            if name not in {'source_search_terms', 'source_search_documents'}))
        export_backup(paths, package)
    unpacked = tmp_path / 'unpacked'; unpacked.mkdir()
    local_backup.unpack(package, unpacked)
    with sqlite3.connect(unpacked / 'knowledge.db') as old:
        assert old.execute('SELECT COUNT(*) FROM source_search_terms').fetchone()[0] > 0
    target = local_paths(tmp_path / 'restored', tmp_path / 'restored-cache', tmp_path / 'restored-logs')
    restore_backup(package, target, 'v' * 48)
    restored = make_engine(f'sqlite:///{target.database}')
    try:
        with Session(restored) as db:
            assert db.scalar(select(SourceSearchDocument)) is None
            services.ensure_search_index(db)
            assert services.search_knowledge(db, identity['owner_id'], SearchRequest(query='合成'))[0]['source_id'] == saved['id']
            assert db.scalar(select(SourceSearchDocument)).owner_id == identity['owner_id']
        with restored.connect() as connection:
            assert migrations.schema_version(connection) == migrations.CURRENT_VERSION
            assert connection.exec_driver_sql('PRAGMA integrity_check').scalar() == 'ok'
            assert connection.exec_driver_sql('PRAGMA foreign_key_check').first() is None
    finally:
        restored.dispose()


def test_reindex_and_worker_delete_leave_no_orphan_cache(local_library):
    _, engine, sessions, identity, client = local_library
    saved = capture_note(local_library)
    with sessions() as db:
        source = db.get(Source, saved['id']); source.title = 'obsoleteprefix'
        services.reindex_source(db, source); db.commit()
        first = db.scalar(select(SourceSearchDocument))
        source.title = 'freshprefix'
        services.reindex_source(db, source); db.commit()
        assert db.scalar(select(SourceSearchDocument)).id == first.id
        assert services.search_knowledge(db, identity['owner_id'], SearchRequest(query='obsolete')) == []
        assert services.search_knowledge(db, identity['owner_id'], SearchRequest(query='fresh'))
    assert client.delete(f"/v1/sources/{saved['id']}", headers={'Authorization': 'Bearer ' + 'j' * 48}).status_code == 202
    assert client.delete(f"/v1/trash/{saved['id']}", headers={'Authorization': 'Bearer ' + 'j' * 48}).status_code == 202
    from services.worker.main import run_once
    with sessions() as db:
        assert run_once(db)
        db.commit()
        assert db.scalar(select(SourceSearchDocument)) is None
        assert db.scalar(select(SourceSearchTerm)) is None
    with engine.connect() as connection:
        assert connection.exec_driver_sql('PRAGMA foreign_key_check').first() is None
