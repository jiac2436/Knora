"""RAG answers are knowledge; executable paid requests stay on their device."""
from contextlib import closing
import json
import sqlite3
import zipfile

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import make_engine
from app.local_backup import export_backup, restore_backup
from app.local_changes import V7_TRIGGER_SQL, validate_change_triggers
from app.local_paths import local_paths
from app.local_readable_export import export_readable
from app.migrations import CURRENT_VERSION, migrate, require_schema
from app.models import RAGJob, RAGSession, RAGTurn, SyncEntityState, User
from app.sync_outbox import prepare_operation, snapshot_entity, replica
from test_local_backup import library

PRIVATE_JOB = 'synthetic-private-gateway-snapshot'


def add_conversation(paths, owner):
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        db.add(RAGSession(id='rag-session', owner_id=owner, title='合成问答'))
        db.flush()
        db.add(RAGTurn(id='rag-turn', owner_id=owner, session_id='rag-session',
            question='资料说了什么？', status='running', request_key='rag-request', request_hash='a' * 64,
            result_json=json.dumps({'answer': '合成回答'}, ensure_ascii=False),
            evidence_json=json.dumps([{'id': 'evidence', 'source_id': 'source',
                'revision_id': 'revision', 'title': '合成资料', 'locator': {'start': 0, 'end': 4}}], ensure_ascii=False)))
        db.flush()
        db.add(RAGJob(id='rag-request', owner_id=owner, turn_id='rag-turn', status='running',
            snapshot_json=json.dumps({'gateway_url': PRIVATE_JOB, 'evidence_text': PRIVATE_JOB})))
    engine.dispose()


def downgrade_seven(engine):
    with engine.begin() as connection:
        from persistence_helpers import remove_domain_processing_schema
        remove_domain_processing_schema(connection)
        for table in ('rag_jobs', 'rag_turns', 'rag_sessions'):
            connection.exec_driver_sql(f'DROP TABLE {table}')
        connection.exec_driver_sql('DELETE FROM schema_migrations WHERE version >= 8')
        require_schema(connection, allow_previous=True)
        validate_change_triggers(connection, version=7)


def test_backup_retains_conversation_without_paid_jobs(tmp_path, library):
    paths, identity, _ = library
    add_conversation(paths, identity['owner_id'])
    package = tmp_path / 'rag.pka'
    export_backup(paths, package)
    with zipfile.ZipFile(package) as archive:
        assert PRIVATE_JOB.encode() not in archive.read('knowledge.db')
    restored = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(package, restored, 'c' * 48)
    with closing(sqlite3.connect(restored.database)) as db:
        assert db.execute('SELECT COUNT(*) FROM rag_jobs').fetchone() == (0,)
        assert db.execute('SELECT question,status FROM rag_turns').fetchone() == ('资料说了什么？', 'running')
        assert db.execute('SELECT title FROM rag_sessions').fetchone() == ('合成问答',)
        assert db.execute("SELECT entity_kind,entity_id FROM sync_entities WHERE entity_kind='rag_session'").fetchone() == ('rag_session', 'rag-session')
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_session_journal_snapshots_answer_but_not_job(library):
    paths, identity, _ = library
    add_conversation(paths, identity['owner_id'])
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        state = db.get(SyncEntityState, ('rag_session', 'rag-session'))
        assert state.generation == 2  # session plus its turn
        operation = prepare_operation(db, 'rag_session', 'rag-session')
        frozen = operation.payload_json
        assert json.loads(frozen)['body']['turns'][0]['id'] == 'rag-turn'
        assert PRIVATE_JOB not in frozen
        db.get(RAGTurn, 'rag-turn').status = 'completed'
        db.flush()
        db.refresh(state)
        assert state.generation == 3
        assert prepare_operation(db, 'rag_session', 'rag-session').payload_json == frozen
        db.get(RAGJob, 'rag-request').status = 'completed'
        db.flush()
        db.refresh(state)
        assert state.generation == 3
    engine.dispose()


def test_readable_export_contains_questions_answers_and_metadata_only(tmp_path, library):
    paths, identity, _ = library
    add_conversation(paths, identity['owner_id'])
    package = tmp_path / 'rag.zip'
    export_readable(paths, package)
    with zipfile.ZipFile(package) as archive:
        metadata = json.loads(archive.read('metadata.json'))
        turn = metadata['rag_sessions'][0]['turns'][0]
        assert turn['status'] == 'unknown'
        assert json.loads(turn['evidence_json'])[0]['id'] == 'evidence'
        markdown = archive.read('rag/000001.md').decode()
        assert '资料说了什么' in markdown and '合成回答' in markdown
        assert PRIVATE_JOB not in '\n'.join(archive.read(name).decode(errors='replace') for name in archive.namelist())


def test_genuine_schema_seven_restore_adds_empty_rag_tables(tmp_path, library):
    paths, _, _ = library
    engine = make_engine(f'sqlite:///{paths.database}')
    downgrade_seven(engine)
    engine.dispose()
    package = tmp_path / 'v7.pka'
    export_backup(paths, package)
    restored = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(package, restored, 'd' * 48)
    with closing(sqlite3.connect(restored.database)) as db:
        assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone() == (CURRENT_VERSION,)
        for table in ('rag_sessions', 'rag_turns', 'rag_jobs'):
            assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone() == (0,)
        assert db.execute('SELECT extracted_text FROM source_revisions').fetchone() == ('原始正文',)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_seven_migration_rejects_changed_old_trigger(library):
    paths, _, _ = library
    engine = make_engine(f'sqlite:///{paths.database}')
    downgrade_seven(engine)
    name = next(iter(V7_TRIGGER_SQL))
    with engine.begin() as connection:
        connection.exec_driver_sql(f'DROP TRIGGER {name}')
        connection.exec_driver_sql(V7_TRIGGER_SQL[name].replace('CURRENT_TIMESTAMP', "'modified'"))
    with pytest.raises(ValueError, match='触发器'):
        migrate(engine, paths.backups)
    with engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT MAX(version) FROM schema_migrations').scalar() == 7
        assert connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE name='rag_sessions'").first() is None
    engine.dispose()


def test_other_owner_conversations_excluded_from_export_and_outbox(tmp_path, library):
    paths, _, _ = library
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        db.add(User(id='other-owner', email='other@example.test', name='合成他人'))
    engine.dispose()
    add_conversation(paths, 'other-owner')
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db:
        assert db.get(SyncEntityState, ('rag_session', 'rag-session')) is None
        with pytest.raises(ValueError, match='当前本地知识库'):
            snapshot_entity(db, 'rag_session', 'rag-session', replica(db))
    engine.dispose()
    package = tmp_path / 'owner.zip'
    export_readable(paths, package)
    with zipfile.ZipFile(package) as archive:
        assert json.loads(archive.read('metadata.json'))['rag_sessions'] == []
        assert not any(name.startswith('rag/') for name in archive.namelist())
