"""AI summary decisions survive local backup, exports and schema upgrades."""
from contextlib import closing
import json
import sqlite3
import zipfile

from sqlalchemy.orm import Session

from app import migrations
from app.database import make_engine
from app.local_backup import export_backup, restore_backup
from app.local_changes import V4_TRIGGER_SQL
from app.local_paths import local_paths
from app.local_readable_export import export_readable
from app.models import AIArtifact, TagRejection, TagSuggestion
from app.sync_outbox import prepare_operation
from test_ai_persistence import add_artifact
from test_local_backup import library


def add_decisions(paths, owner):
    add_artifact(paths, owner)
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        artifact = db.get(AIArtifact, 'artifact')
        result = json.loads(artifact.result_json)
        result.update(contract_version=2, overview={'text': '本地概述', 'citations': ['evidence']},
                      limitations=[{'text': '原文限制', 'citations': ['evidence']}])
        artifact.result_json = json.dumps(result, ensure_ascii=False)
        db.add(TagSuggestion(id='candidate', owner_id=owner, source_id='source', artifact_id='artifact',
                            revision_id='revision', name='候选主题', normalized_name='候选主题',
                            dimension='topic', reason='证据理由', evidence_json='["evidence"]', status='rejected'))
        db.add(TagRejection(source_id='source', revision_id='revision', dimension='topic', normalized_name='候选主题'))
    engine.dispose()


def test_backup_and_readable_export_keep_summary_and_decisions(tmp_path, library):
    paths, identity, _ = library
    add_decisions(paths, identity['owner_id'])
    backup = tmp_path / 'decisions.pka'
    export_backup(paths, backup)
    restored = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(backup, restored, 'b' * 48)
    with closing(sqlite3.connect(restored.database)) as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('SELECT status FROM tag_suggestions').fetchone() == ('rejected',)
        assert db.execute('SELECT normalized_name FROM tag_rejections').fetchone() == ('候选主题',)
        assert db.execute('SELECT COUNT(*) FROM ai_jobs').fetchone()[0] == 0
    readable = tmp_path / 'decisions.zip'
    export_readable(paths, readable)
    with zipfile.ZipFile(readable) as archive:
        metadata = json.loads(archive.read('metadata.json'))['sources'][0]
        assert metadata['tag_suggestions'][0]['status'] == 'rejected'
        assert metadata['tag_rejections'][0]['normalized_name'] == '候选主题'
        markdown = archive.read('sources/000001.md').decode()
        assert all(text in markdown for text in ('一句话概述', '本地概述', '原文限制', '候选主题', 'rejected'))
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db:
        prepare_operation(db, 'domain', 'domain'); db.commit()
        body = json.loads(prepare_operation(db, 'source', 'source').payload_json)['body']
        assert body['tag_suggestions'][0]['id'] == 'candidate'
        assert body['tag_rejections'][0]['normalized_name'] == '候选主题'
    engine.dispose()


def test_v3_backup_keeps_automatic_and_manual_labels_without_paid_tasks(tmp_path, library):
    from app.models import TagAssignment
    from test_tag_persistence import add_labels

    paths, identity, _ = library
    add_decisions(paths, identity['owner_id'])
    add_labels(paths, identity['owner_id'])
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        artifact = db.get(AIArtifact, 'artifact')
        result = json.loads(artifact.result_json)
        result['contract_version'] = 3
        artifact.result_json = json.dumps(result, ensure_ascii=False)
        assignment = db.get(TagAssignment, ('source', 'tag-one', 'ai'))
        assignment.artifact_id = artifact.id
    engine.dispose()
    backup = tmp_path / 'automatic.pka'
    export_backup(paths, backup)
    restored = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(backup, restored, 'b' * 48)
    with closing(sqlite3.connect(restored.database)) as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        result = json.loads(db.execute('SELECT result_json FROM ai_artifacts').fetchone()[0])
        assert result['contract_version'] == 3 and result['overview']['text'] == '本地概述'
        assert db.execute('SELECT origin,artifact_id FROM tag_assignments ORDER BY origin').fetchall() == [
            ('ai', 'artifact'), ('manual', None)]
        assert db.execute('SELECT COUNT(*) FROM ai_jobs').fetchone() == (0,)
    readable = tmp_path / 'automatic.zip'
    export_readable(paths, readable)
    with zipfile.ZipFile(readable) as archive:
        body = json.loads(archive.read('metadata.json'))['sources'][0]
        assert json.loads(body['ai_artifacts'][0]['result_json'])['contract_version'] == 3
        assert '本地概述' in archive.read('sources/000001.md').decode()


def test_genuine_v4_backup_upgrades_without_rewriting_labels(tmp_path, library):
    paths, identity, _ = library
    from test_tag_persistence import add_labels
    add_labels(paths, identity['owner_id'])
    engine = make_engine(f'sqlite:///{paths.database}')
    with engine.begin() as connection:
        from persistence_helpers import remove_domain_processing_schema
        remove_domain_processing_schema(connection)
        names = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'").scalars().all()
        for name in names:
            if name not in V4_TRIGGER_SQL:
                connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
        for table in ('rag_jobs', 'rag_turns', 'rag_sessions', 'summary_search_terms', 'summary_embeddings', 'summary_index_jobs', 'ai_job_steps', 'tag_suggestions', 'tag_rejections'):
            connection.exec_driver_sql(f'DROP TABLE {table}')
        connection.exec_driver_sql('DELETE FROM schema_migrations WHERE version>=5')
        migrations.require_schema(connection, allow_previous=True)
    engine.dispose()
    backup = tmp_path / 'v4.pka'
    export_backup(paths, backup)
    restored = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(backup, restored, 'c' * 48)
    with closing(sqlite3.connect(restored.database)) as db:
        assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone() == (migrations.CURRENT_VERSION,)
        assert db.execute('SELECT COUNT(*) FROM tag_suggestions').fetchone() == (0,)
        assert db.execute('SELECT COUNT(*) FROM tag_assignments').fetchone() == (2,)
        assert db.execute('SELECT normalized_name FROM knowledge_tag_aliases').fetchone() == ('rag',)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_purge_removes_summary_candidates_and_rejections(library, monkeypatch):
    from dataclasses import replace
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.models import Source
    from services.worker import main as worker
    paths, identity, _ = library
    add_decisions(paths, identity['owner_id'])
    monkeypatch.setattr(worker, 'settings', replace(worker.settings, environment='local',
        database_url=f'sqlite:///{paths.database}', local_data_dir=str(paths.data),
        local_cache_dir=str(paths.cache), local_log_dir=str(paths.logs), upload_dir=paths.assets))
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db:
        source = db.get(Source, 'source')
        source.deleted_at = datetime.now(timezone.utc)
        source.processing_status = 'deleting'; db.commit()
        assert worker.run_once(db)
        assert db.get(AIArtifact, 'artifact') is None
        assert not db.scalars(select(TagSuggestion)).all()
        assert not db.scalars(select(TagRejection)).all()
        assert db.get(Source, 'source').processing_status == 'deleted'
    engine.dispose()
