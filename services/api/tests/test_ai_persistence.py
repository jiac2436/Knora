"""AI knowledge survives exports; task execution and device state do not."""
from contextlib import closing
import json
import sqlite3
import zipfile

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import make_engine
from app.local_backup import export_backup, restore_backup, verify_backup
from app.local_changes import TRIGGER_PREFIX, V2_TRIGGER_SQL
from app.local_paths import local_paths
from app.local_readable_export import export_readable
from app.migrations import CURRENT_VERSION, migrate, require_schema
from app.models import AIArtifact, AIJob, Domain, Source, SyncEntityState
from app.sync_outbox import prepare_operation
from test_local_backup import library


def add_artifact(paths, owner):
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        db.add(AIJob(id='ai-job', owner_id=owner, source_id='source', kind='analyze',
                     status='running', request_key='request-one', request_sha256='f' * 64,
                     snapshot_json='{"private_task_marker":"not-exported"}'))
        db.add(AIArtifact(id='artifact', owner_id=owner, source_id='source', job_id='ai-job',
                          revision_id='revision', result_json=json.dumps({
                              'summary': '本地分析产物', 'summary_citations': ['evidence'],
                              'insights': [], 'organizing_suggestion': None, 'warnings': []}, ensure_ascii=False),
                          snapshot_json='{"revision_id":"revision","evidence":[]}',
                          provider='deepseek', model='synthetic-model', prompt_version='single-source-v1',
                          usage_json='{"prompt_tokens":5,"completion_tokens":3}'))
    engine.dispose()


def test_backup_keeps_artifact_excludes_paid_job_and_restores(tmp_path, library):
    paths, identity, _ = library
    add_artifact(paths, identity['owner_id'])
    package = tmp_path / 'ai.pka'
    export_backup(paths, package)
    verify_backup(package)
    with zipfile.ZipFile(package) as archive:
        data = archive.read('knowledge.db')
        assert b'private_task_marker' not in data
        database = tmp_path / 'inspection.db'
        database.write_bytes(data)
    with closing(sqlite3.connect(database)) as db:
        assert db.execute('SELECT COUNT(*) FROM ai_jobs').fetchone()[0] == 0
        assert db.execute('SELECT id,job_id FROM ai_artifacts').fetchone() == ('artifact', 'ai-job')
    restored = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(package, restored, 'b' * 48)
    with closing(sqlite3.connect(restored.database)) as db:
        assert db.execute('SELECT COUNT(*) FROM ai_jobs').fetchone()[0] == 0
        assert db.execute('SELECT result_json FROM ai_artifacts').fetchone()[0].find('本地分析产物') >= 0


def test_readable_export_includes_ai_knowledge_without_job_snapshot(tmp_path, library):
    paths, identity, _ = library
    add_artifact(paths, identity['owner_id'])
    package = tmp_path / 'ai.zip'
    export_readable(paths, package)
    with zipfile.ZipFile(package) as archive:
        metadata = json.loads(archive.read('metadata.json'))
        assert len(metadata['sources'][0]['ai_artifacts']) == 1
        assert '本地分析产物' in archive.read('sources/000001.md').decode()
        assert all(b'private_task_marker' not in archive.read(name) for name in archive.namelist())


def test_artifact_is_journaled_and_fixed_outbox_includes_it(library):
    paths, identity, _ = library
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db:
        before = db.get(SyncEntityState, ('source', 'source')).generation
    add_artifact(paths, identity['owner_id'])
    with Session(engine) as db:
        assert db.get(SyncEntityState, ('source', 'source')).generation == before + 1
        prepare_operation(db, 'domain', 'domain')
        db.commit()
        op = prepare_operation(db, 'source', 'source')
        body = json.loads(op.payload_json)['body']
        assert body['ai_artifacts'][0]['id'] == 'artifact'
        assert 'ai_jobs' not in body
        assert 'private_task_marker' not in op.payload_json
    engine.dispose()


def downgrade_v2(engine):
    with engine.begin() as connection:
        from persistence_helpers import remove_domain_processing_schema
        remove_domain_processing_schema(connection)
        for name in list(connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'").scalars()):
            if name not in V2_TRIGGER_SQL:
                connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
        for table in ('rag_jobs', 'rag_turns', 'rag_sessions', 'summary_search_terms', 'summary_embeddings', 'summary_index_jobs'):
            connection.exec_driver_sql(f'DROP TABLE {table}')
        connection.exec_driver_sql('DROP TABLE ai_job_steps')
        connection.exec_driver_sql('DROP TABLE ai_jobs')
        connection.exec_driver_sql('DROP TABLE ai_artifacts')
        for table in ('tag_suggestions', 'tag_rejections', 'tag_assignments', 'knowledge_tag_aliases', 'knowledge_tags'):
            connection.exec_driver_sql(f'DROP TABLE {table}')
        connection.exec_driver_sql('DELETE FROM schema_migrations WHERE version>=3')
        require_schema(connection, allow_previous=True)


def test_genuine_v2_package_restores_and_upgrades(tmp_path, library):
    paths, identity, _ = library
    engine = make_engine(f'sqlite:///{paths.database}')
    downgrade_v2(engine)
    engine.dispose()
    package = tmp_path / 'v2.pka'
    export_backup(paths, package)
    restored = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(package, restored, 'c' * 48)
    with closing(sqlite3.connect(restored.database)) as db:
        assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == CURRENT_VERSION
        assert db.execute('SELECT COUNT(*) FROM ai_artifacts').fetchone()[0] == 0
        assert db.execute('SELECT id FROM sources').fetchone()[0] == 'source'


def test_v2_upgrade_rejects_modified_old_trigger_before_adding_ai(library):
    paths, _, _ = library
    engine = make_engine(f'sqlite:///{paths.database}')
    downgrade_v2(engine)
    with engine.begin() as connection:
        name = next(iter(V2_TRIGGER_SQL))
        connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
        connection.exec_driver_sql(f'CREATE TRIGGER {name} AFTER INSERT ON sources BEGIN SELECT 1; END')
    with pytest.raises(ValueError):
        migrate(engine, paths.backups)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM sqlite_master WHERE name='ai_artifacts'").scalar() == 0
        assert connection.exec_driver_sql('SELECT MAX(version) FROM schema_migrations').scalar() == 2
    engine.dispose()


def test_worker_restart_marks_running_call_unknown_without_replay(tmp_path, library):
    import os
    import subprocess
    import sys
    paths, identity, _ = library
    add_artifact(paths, identity['owner_id'])
    # Remove unrelated synthetic work so --once only handles AI restart recovery.
    with closing(sqlite3.connect(paths.database)) as db:
        for table in ('deletion_jobs', 'document_jobs', 'ocr_jobs', 'web_read_jobs', 'semantic_index_jobs'):
            db.execute(f'DELETE FROM {table}')
        db.commit()
    env = dict(os.environ, PYTHONPATH='services/api:.', PKA_ENVIRONMENT='local',
               PKA_DATA_DIR=str(paths.data), PKA_CACHE_DIR=str(paths.cache),
               PKA_LOG_DIR=str(paths.logs), PKA_SEMANTIC_ENABLED='0')
    for key in ('PKA_DATABASE_URL', 'PKA_UPLOAD_DIR', 'PKA_AI_GATEWAY_URL', 'PKA_AI_GATEWAY_TOKEN'):
        env.pop(key, None)
    result = subprocess.run([sys.executable, '-m', 'services.worker.main', '--once'],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    with closing(sqlite3.connect(paths.database)) as db:
        assert db.execute('SELECT status FROM ai_jobs WHERE id=?', ('ai-job',)).fetchone()[0] == 'unknown'
        assert db.execute('SELECT COUNT(*) FROM ai_artifacts').fetchone()[0] == 1
