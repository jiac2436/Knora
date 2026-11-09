"""Disposable summary indexes are rebuilt locally and never portable knowledge."""
from contextlib import closing
import sqlite3

from sqlalchemy.orm import Session

from app import migrations
from app.database import make_engine
from app.local_backup import export_backup, restore_backup
from app.local_paths import local_paths
from app.models import SummaryIndexJob, SummarySearchTerm, SummaryEmbedding
from test_ai_persistence import add_artifact
from test_local_backup import library


def indexes(paths, owner):
    add_artifact(paths, owner)
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        db.add(SummaryIndexJob(source_id='source', artifact_id='artifact', status='completed'))
        db.add(SummarySearchTerm(source_id='source', artifact_id='artifact', owner_id=owner, term='分析'))
        db.add(SummaryEmbedding(source_id='source', artifact_id='artifact', owner_id=owner,
            revision_id='revision', ordinal=0, start=0, end=4, model='synthetic', vector=b'\x00' * 8))
    engine.dispose()


def test_backup_preserves_summary_and_removes_derived_indexes(tmp_path, library):
    paths, identity, _ = library
    indexes(paths, identity['owner_id'])
    package = tmp_path / 'summary.pka'
    export_backup(paths, package)
    restored = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(package, restored, 'b' * 48)
    with closing(sqlite3.connect(restored.database)) as db:
        for table in ('summary_index_jobs', 'summary_search_terms', 'summary_embeddings'):
            assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone() == (0,)
        assert db.execute('SELECT id FROM ai_artifacts').fetchone() == ('artifact',)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_genuine_schema_six_backup_upgrades_preserving_summary(tmp_path, library):
    paths, identity, _ = library
    indexes(paths, identity['owner_id'])
    engine = make_engine(f'sqlite:///{paths.database}')
    with engine.begin() as connection:
        from persistence_helpers import remove_domain_processing_schema
        remove_domain_processing_schema(connection)
        for table in ('rag_jobs', 'rag_turns', 'rag_sessions', 'summary_search_terms', 'summary_embeddings', 'summary_index_jobs'):
            connection.exec_driver_sql(f'DROP TABLE {table}')
        connection.exec_driver_sql('DELETE FROM schema_migrations WHERE version>=7')
        migrations.require_schema(connection, allow_previous=True)
    engine.dispose()
    package = tmp_path / 'v6.pka'
    export_backup(paths, package)
    restored = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(package, restored, 'c' * 48)
    with closing(sqlite3.connect(restored.database)) as db:
        assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone() == (migrations.CURRENT_VERSION,)
        assert db.execute('SELECT id FROM ai_artifacts').fetchone() == ('artifact',)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
