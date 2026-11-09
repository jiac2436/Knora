"""Long-analysis knowledge is portable; executable checkpoints stay on-device.

All requests, provider names and credential-shaped markers are synthetic. These
checks never call a model or read a real credential.
"""
from contextlib import closing
from dataclasses import replace
import json
import os
import sqlite3
import subprocess
import sys
import zipfile

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import make_engine
from app.local_backup import export_backup, restore_backup, verify_backup
from app.local_paths import local_paths
from app.local_readable_export import export_readable
from app.migrations import CURRENT_VERSION, migrate, require_schema
from app.models import AIArtifact, AIJob, AIJobStep, DeletionJob, Source, SyncEntityState, TagRejection, utcnow
from app.sync_outbox import prepare_operation
from services.worker import main as worker
from test_local_backup import library

PRIVATE_MARKER = 'synthetic-long-request-credential-never-export'


def add_long_analysis(paths, owner):
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        db.add(AIJob(id='long-job', owner_id=owner, source_id='source', kind='analyze', status='running',
            request_key='long-request', request_sha256='e' * 64,
            snapshot_json=json.dumps({'task_marker': PRIVATE_MARKER})))
        db.flush()
        for ordinal, status in enumerate(('completed', 'running', 'pending')):
            db.add(AIJobStep(id=f'long-step-{ordinal}', job_id='long-job', ordinal=ordinal,
                phase='segment' if ordinal < 2 else 'synthesis', status=status,
                request_json=json.dumps({'authorization': PRIVATE_MARKER}),
                response_json=json.dumps({'checkpoint': PRIVATE_MARKER}) if status == 'completed' else None))
        db.add(AIArtifact(id='segment-artifact', owner_id=owner, source_id='source',
            job_id='long-step-0', revision_id='revision', provider='synthetic', model='synthetic-model',
            prompt_version='synthetic-long-v1', usage_json='{"prompt_tokens":20,"completion_tokens":8}',
            snapshot_json=json.dumps({'revision_id': 'revision', 'evidence': [{'id': 'evidence', 'text': '原始正文'}],
                                      'phase': 'segment', 'section_index': 1,
                                      'analysis_plan': {'coverage': 'partial', 'source_coverage': 'partial_text',
                                                        'covered_chars': 4, 'total_chars': 8}}),
            result_json=json.dumps({'summary': '已完成分段的本地总结', 'summary_citations': ['evidence'],
                'insights': [], 'organizing_suggestion': None, 'warnings': []}, ensure_ascii=False)))
        db.add(TagRejection(source_id='source', revision_id='revision', dimension='topic', normalized_name='rejected-tag'))
    engine.dispose()


def downgrade_v5(paths):
    engine = make_engine(f'sqlite:///{paths.database}')
    with engine.begin() as connection:
        from persistence_helpers import remove_domain_processing_schema
        remove_domain_processing_schema(connection)
        for table in ('rag_jobs', 'rag_turns', 'rag_sessions', 'summary_search_terms', 'summary_embeddings', 'summary_index_jobs'):
            connection.exec_driver_sql(f'DROP TABLE {table}')
        connection.exec_driver_sql('DROP TABLE ai_job_steps')
        connection.exec_driver_sql('DELETE FROM schema_migrations WHERE version > 5')
        require_schema(connection, allow_previous=True)
    engine.dispose()


def test_schema_five_upgrade_preserves_knowledge_and_adds_empty_checkpoints(library):
    paths, identity, _ = library
    add_long_analysis(paths, identity['owner_id'])
    downgrade_v5(paths)
    engine = make_engine(f'sqlite:///{paths.database}')
    snapshot = migrate(engine, paths.backups)
    assert snapshot is not None and snapshot.is_file()
    with engine.connect() as connection:
        require_schema(connection)
        assert connection.exec_driver_sql('SELECT MAX(version) FROM schema_migrations').scalar() == CURRENT_VERSION
        assert connection.exec_driver_sql('SELECT COUNT(*) FROM ai_job_steps').scalar() == 0
        assert connection.exec_driver_sql('SELECT id FROM ai_artifacts').scalar() == 'segment-artifact'
        assert connection.exec_driver_sql('SELECT COUNT(*) FROM tag_rejections').scalar() == 1
    engine.dispose()


def test_genuine_schema_five_backup_restores_without_paid_work(tmp_path, library):
    paths, identity, _ = library
    add_long_analysis(paths, identity['owner_id'])
    downgrade_v5(paths)
    package = tmp_path / 'schema-five.pka'
    export_backup(paths, package)
    verify_backup(package)
    restored = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(package, restored, 'b' * 48)
    with closing(sqlite3.connect(restored.database)) as db:
        assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == CURRENT_VERSION
        assert db.execute('SELECT COUNT(*) FROM ai_jobs').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM ai_job_steps').fetchone()[0] == 0
        assert db.execute('SELECT id,job_id FROM ai_artifacts').fetchone() == ('segment-artifact', 'long-step-0')
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_backup_keeps_segment_knowledge_excludes_checkpoint_credentials(tmp_path, library):
    paths, identity, _ = library
    add_long_analysis(paths, identity['owner_id'])
    package = tmp_path / 'long.pka'
    export_backup(paths, package)
    verify_backup(package)
    with zipfile.ZipFile(package) as archive:
        exported = archive.read('knowledge.db')
        assert PRIVATE_MARKER.encode() not in exported
        inspection = tmp_path / 'inspection.db'
        inspection.write_bytes(exported)
    with closing(sqlite3.connect(inspection)) as db:
        assert db.execute('SELECT COUNT(*) FROM ai_jobs').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM ai_job_steps').fetchone()[0] == 0
        assert '已完成分段的本地总结' in db.execute('SELECT result_json FROM ai_artifacts').fetchone()[0]
    restored = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(package, restored, 'c' * 48)
    with closing(sqlite3.connect(restored.database)) as db:
        assert db.execute('SELECT COUNT(*) FROM ai_jobs').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM ai_job_steps').fetchone()[0] == 0
        assert db.execute('SELECT job_id FROM ai_artifacts').fetchone()[0] == 'long-step-0'


def test_readable_export_keeps_partial_result_without_checkpoints(tmp_path, library):
    paths, identity, _ = library
    add_long_analysis(paths, identity['owner_id'])
    package = tmp_path / 'long-readable.zip'
    export_readable(paths, package)
    with zipfile.ZipFile(package) as archive:
        assert all(PRIVATE_MARKER.encode() not in archive.read(name) for name in archive.namelist())
        metadata = json.loads(archive.read('metadata.json'))
        artifact = metadata['sources'][0]['ai_artifacts'][0]
        assert artifact['job_id'] == 'long-step-0'
        markdown = archive.read('sources/000001.md').decode()
        assert '已完成分段的本地总结' in markdown
        assert '分段总结（非全文）' in markdown
        assert '原资料采集范围：partial_text' in markdown
        assert '证据覆盖：partial' in markdown
        assert 'ai_jobs' not in metadata['sources'][0] and 'ai_job_steps' not in metadata['sources'][0]


def test_steps_do_not_journal_or_enter_sync_payload(library):
    paths, identity, _ = library
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db:
        before = db.get(SyncEntityState, ('source', 'source')).generation
    add_long_analysis(paths, identity['owner_id'])
    with Session(engine) as db:
        # Only the durable segment artifact and rejection are business changes.
        after = db.get(SyncEntityState, ('source', 'source')).generation
        assert after == before + 2
        step = db.get(AIJobStep, 'long-step-1')
        step.status = 'unknown'
        step.error = PRIVATE_MARKER
        db.commit()
        db.expire_all()
        assert db.get(SyncEntityState, ('source', 'source')).generation == after
        prepare_operation(db, 'domain', 'domain'); db.commit()
        operation = prepare_operation(db, 'source', 'source')
        body = json.loads(operation.payload_json)['body']
        assert body['ai_artifacts'][0]['id'] == 'segment-artifact'
        assert 'ai_job_steps' not in body and 'ai_jobs' not in body
        assert PRIVATE_MARKER not in operation.payload_json
    engine.dispose()


def test_purge_cleans_checkpoints_artifacts_and_rejections(library, monkeypatch):
    paths, identity, _ = library
    add_long_analysis(paths, identity['owner_id'])
    monkeypatch.setattr(worker, 'settings', replace(worker.settings, environment='local',
        database_url=f'sqlite:///{paths.database}', local_data_dir=str(paths.data),
        local_cache_dir=str(paths.cache), local_log_dir=str(paths.logs), upload_dir=paths.assets))
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db:
        source = db.get(Source, 'source')
        source.deleted_at, source.processing_status = utcnow(), 'deleting'
        db.commit()
        assert worker.run_once(db)
        assert db.get(DeletionJob, 'delete').status == 'completed'
        for model in (AIJobStep, AIJob, AIArtifact, TagRejection):
            assert db.scalars(select(model)).all() == []
        assert db.get(Source, 'source').processing_status == 'deleted'
    engine.dispose()


def test_worker_restart_retains_completed_segment_and_never_replays_unknown(library):
    paths, identity, _ = library
    add_long_analysis(paths, identity['owner_id'])
    with closing(sqlite3.connect(paths.database)) as db:
        for table in ('deletion_jobs', 'document_jobs', 'ocr_jobs', 'web_read_jobs', 'semantic_index_jobs'):
            db.execute(f'DELETE FROM {table}')
        db.commit()
    env = dict(os.environ, PYTHONPATH='services/api:.', PKA_ENVIRONMENT='local',
        PKA_DATA_DIR=str(paths.data), PKA_CACHE_DIR=str(paths.cache), PKA_LOG_DIR=str(paths.logs),
        PKA_SEMANTIC_ENABLED='0')
    for key in ('PKA_DATABASE_URL', 'PKA_UPLOAD_DIR', 'PKA_AI_GATEWAY_URL', 'PKA_AI_GATEWAY_TOKEN'):
        env.pop(key, None)
    result = subprocess.run([sys.executable, '-m', 'services.worker.main', '--once'],
        env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    with closing(sqlite3.connect(paths.database)) as db:
        assert db.execute('SELECT status FROM ai_jobs').fetchone()[0] == 'unknown'
        statuses = db.execute('SELECT status FROM ai_job_steps ORDER BY ordinal').fetchall()
        assert statuses == [('completed',), ('unknown',), ('pending',)]
        assert db.execute('SELECT id FROM ai_artifacts').fetchone()[0] == 'segment-artifact'


def test_schema_five_modified_trigger_fails_before_creating_checkpoints(library):
    paths, _, _ = library
    downgrade_v5(paths)
    engine = make_engine(f'sqlite:///{paths.database}')
    with engine.begin() as connection:
        name = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name LIMIT 1").scalar()
        connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
        connection.exec_driver_sql(f'CREATE TRIGGER "{name}" AFTER INSERT ON sources BEGIN SELECT 1; END')
    with pytest.raises(ValueError):
        migrate(engine, paths.backups)
    with engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT MAX(version) FROM schema_migrations').scalar() == 5
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM sqlite_master WHERE name='ai_job_steps'").scalar() == 0
    engine.dispose()
