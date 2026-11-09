"""Formal labels remain knowledge through indexes, local exports and deletion."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3
import zipfile

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import migrations, services
from app.database import make_engine
from app.local_backup import export_backup, restore_backup
from app.local_changes import V3_TRIGGER_SQL
from app.local_paths import local_paths
from app.local_readable_export import export_readable
from app.models import (DeletionJob, KnowledgeTag, KnowledgeTagAlias, LocalChange,
                        Source, SourceTag, SyncEntityState, TagAssignment, User)
from app.sync_outbox import prepare_operation
from services.worker import main as worker
from test_local_backup import library


def add_labels(paths, owner):
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        db.add(KnowledgeTag(id='topic-root', owner_id=owner, dimension='topic',
                           name='人工智能', normalized_name='人工智能'))
        db.flush()
        db.add(KnowledgeTag(id='tag-one', owner_id=owner, dimension='topic',
                           name='检索增强生成', normalized_name='检索增强生成', parent_id='topic-root'))
        db.flush()
        db.add(KnowledgeTagAlias(id='tag-alias', owner_id=owner, dimension='topic',
                                tag_id='tag-one', name='RAG', normalized_name='rag'))
        db.add(TagAssignment(source_id='source', tag_id='tag-one', origin='manual'))
        db.add(TagAssignment(source_id='source', tag_id='tag-one', origin='ai',
                             revision_id='revision', artifact_id='historical-artifact',
                             evidence_json='["evidence"]'))
        db.add(SourceTag(source_id='source', name='规则关键词'))
    engine.dispose()


def test_backup_round_trip_keeps_alias_parent_and_both_origins(tmp_path, library):
    paths, identity, _ = library
    add_labels(paths, identity['owner_id'])
    package = tmp_path / 'labels.pka'
    export_backup(paths, package)
    restored = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restored_identity = restore_backup(package, restored, 'b' * 48)
    assert restored_identity['device_id'] != identity['device_id']
    with closing(sqlite3.connect(restored.database)) as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('SELECT parent_id FROM knowledge_tags WHERE id=?', ('tag-one',)).fetchone() == ('topic-root',)
        assert db.execute('SELECT name,tag_id FROM knowledge_tag_aliases').fetchone() == ('RAG', 'tag-one')
        assignments = db.execute('SELECT origin,revision_id,evidence_json FROM tag_assignments ORDER BY origin').fetchall()
        assert assignments == [('ai', 'revision', '["evidence"]'), ('manual', None, '[]')]
        assert db.execute('SELECT COUNT(*) FROM ai_jobs').fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM sync_entities WHERE entity_kind='tag'").fetchone()[0] == 2


def test_alias_edit_is_journaled_transactionally_and_other_owner_is_excluded(library):
    paths, identity, _ = library
    add_labels(paths, identity['owner_id'])
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db:
        before = db.get(SyncEntityState, ('tag', 'tag-one')).generation
        alias = db.get(KnowledgeTagAlias, 'tag-alias')
        alias.name = 'RAG 检索'
        alias.normalized_name = 'rag 检索'
        db.flush()
        assert db.get(SyncEntityState, ('tag', 'tag-one')).generation == before + 1
        db.rollback()
        assert db.get(SyncEntityState, ('tag', 'tag-one')).generation == before
        assert db.get(KnowledgeTagAlias, 'tag-alias').name == 'RAG'
        db.add(User(id='other-owner', email='other@example.test', name='Other'))
        db.flush()
        db.add(KnowledgeTag(id='other-tag', owner_id='other-owner', dimension='topic',
                           name='隔离标签', normalized_name='隔离标签'))
        db.commit()
        assert db.get(SyncEntityState, ('tag', 'other-tag')) is None
        assert not db.scalars(select(LocalChange).where(LocalChange.entity_id == 'other-tag')).all()
        with pytest.raises(ValueError):
            prepare_operation(db, 'tag', 'other-tag')
    engine.dispose()


def test_assignment_changes_source_generation_and_outbox_requires_tag_dependency(library):
    paths, identity, _ = library
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db:
        original = db.get(SyncEntityState, ('source', 'source')).generation
    add_labels(paths, identity['owner_id'])
    with Session(engine) as db:
        assert db.get(SyncEntityState, ('source', 'source')).generation == original + 2
        prepare_operation(db, 'domain', 'domain')
        db.commit()
        with pytest.raises(ValueError):
            prepare_operation(db, 'source', 'source')
        db.rollback()
        with pytest.raises(ValueError):
            prepare_operation(db, 'tag', 'tag-one')
        db.rollback()
        parent = prepare_operation(db, 'tag', 'topic-root')
        db.commit()
        tag = prepare_operation(db, 'tag', 'tag-one')
        db.commit()
        tag_body = json.loads(tag.payload_json)['body']
        assert tag_body['dependencies'] == [{'entity_kind': 'tag', 'entity_id': 'topic-root', 'operation_id': parent.id}]
        assert tag_body['aliases'][0]['name'] == 'RAG'
        operation = prepare_operation(db, 'source', 'source')
        body = json.loads(operation.payload_json)['body']
        assert {item['origin'] for item in body['tag_assignments']} == {'ai', 'manual'}
        assert {'entity_kind': 'tag', 'entity_id': 'tag-one', 'operation_id': tag.id} in body['dependencies']
    engine.dispose()


def test_reindex_preserves_confirmed_formal_labels(library):
    paths, identity, _ = library
    add_labels(paths, identity['owner_id'])
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db:
        services.reindex_source(db, db.get(Source, 'source'))
        db.commit()
        labels = db.scalars(select(TagAssignment).where(TagAssignment.source_id == 'source')).all()
        assert {label.origin for label in labels} == {'manual', 'ai'}
        assert next(label for label in labels if label.origin == 'ai').evidence_json == '["evidence"]'
        assert db.get(KnowledgeTagAlias, 'tag-alias').name == 'RAG'
    engine.dispose()


def test_purge_removes_assignments_but_keeps_shared_vocabulary(library, monkeypatch):
    paths, identity, _ = library
    add_labels(paths, identity['owner_id'])
    monkeypatch.setattr(worker, 'settings', replace(worker.settings, environment='local',
                                                   database_url=f'sqlite:///{paths.database}',
                                                   local_data_dir=str(paths.data),
                                                   local_cache_dir=str(paths.cache),
                                                   local_log_dir=str(paths.logs),
                                                   upload_dir=paths.assets))
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db:
        source = db.get(Source, 'source')
        source.deleted_at = datetime.now(timezone.utc)
        source.processing_status = 'deleting'
        db.commit()
        assert worker.run_once(db)
        assert db.get(DeletionJob, 'delete').status == 'completed'
        assert not db.scalars(select(TagAssignment).where(TagAssignment.source_id == 'source')).all()
        assert db.get(KnowledgeTag, 'tag-one') is not None
        assert db.get(KnowledgeTagAlias, 'tag-alias') is not None
        assert db.get(Source, 'source').processing_status == 'deleted'
    engine.dispose()


def test_readable_export_separates_formal_and_rule_tags_and_excludes_other_owner(tmp_path, library):
    paths, identity, _ = library
    add_labels(paths, identity['owner_id'])
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db, db.begin():
        db.add(User(id='other-owner', email='other@example.test', name='Other'))
        db.flush()
        db.add(KnowledgeTag(id='other-tag', owner_id='other-owner', dimension='topic',
                           name='私有标签', normalized_name='私有标签'))
    engine.dispose()
    package = tmp_path / 'readable.zip'
    export_readable(paths, package)
    with zipfile.ZipFile(package) as archive:
        metadata = json.loads(archive.read('metadata.json'))
        assert {item['id'] for item in metadata['knowledge_tags']} == {'topic-root', 'tag-one'}
        tag = next(item for item in metadata['knowledge_tags'] if item['id'] == 'tag-one')
        assert tag['aliases'][0]['name'] == 'RAG'
        source = metadata['sources'][0]
        assert source['legacy_rule_tags'] == ['规则关键词']
        assert {item['origin'] for item in source['tag_assignments']} == {'manual', 'ai'}
        text = archive.read('sources/000001.md').decode()
        assert '检索增强生成' in text
        assert '私有标签' not in archive.read('metadata.json').decode()


def downgrade_v3(engine):
    with engine.begin() as connection:
        from persistence_helpers import remove_domain_processing_schema
        remove_domain_processing_schema(connection)
        triggers = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='trigger'").scalars().all()
        for name in triggers:
            if name not in V3_TRIGGER_SQL:
                connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
        for table in ('rag_jobs', 'rag_turns', 'rag_sessions', 'summary_search_terms', 'summary_embeddings', 'summary_index_jobs', 'ai_job_steps', 'tag_suggestions', 'tag_rejections', 'tag_assignments', 'knowledge_tag_aliases', 'knowledge_tags'):
            connection.exec_driver_sql(f'DROP TABLE {table}')
        connection.exec_driver_sql('DELETE FROM schema_migrations WHERE version>=4')
        migrations.require_schema(connection, allow_previous=True)


def test_genuine_v3_backup_restores_and_upgrades_without_rewriting_originals(tmp_path, library):
    paths, identity, _ = library
    engine = make_engine(f'sqlite:///{paths.database}')
    downgrade_v3(engine)
    engine.dispose()
    package = tmp_path / 'v3.pka'
    export_backup(paths, package)
    restored = local_paths(tmp_path / 'restored', tmp_path / 'cache', tmp_path / 'logs')
    restore_backup(package, restored, 'c' * 48)
    with closing(sqlite3.connect(restored.database)) as db:
        assert db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == migrations.CURRENT_VERSION
        assert db.execute('SELECT COUNT(*) FROM knowledge_tags').fetchone()[0] == 0
        assert db.execute('SELECT id,version,extracted_text FROM source_revisions').fetchone() == ('revision', 7, '原始正文')
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_v3_upgrade_rejects_modified_trigger_before_adding_formal_tag_tables(library):
    paths, _, _ = library
    engine = make_engine(f'sqlite:///{paths.database}')
    downgrade_v3(engine)
    with engine.begin() as connection:
        name = next(iter(V3_TRIGGER_SQL))
        connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
        connection.exec_driver_sql(f'CREATE TRIGGER {name} AFTER INSERT ON sources BEGIN SELECT 1; END')
    with pytest.raises(ValueError):
        migrations.migrate(engine, paths.backups)
    with engine.connect() as connection:
        assert connection.exec_driver_sql('SELECT MAX(version) FROM schema_migrations').scalar() == 3
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM sqlite_master WHERE name='knowledge_tags'").scalar() == 0
    engine.dispose()
