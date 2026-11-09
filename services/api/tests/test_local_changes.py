"""Transactional local changes on an isolated, migrated SQLite library."""
from dataclasses import replace
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app import auth, files, local_trash, main, services
from app.database import get_db, make_engine
from app.local_paths import local_paths
from app.local_runtime import initialize_identity
from app.migrations import migrate
from app.models import Domain, PersonalContext, Project, Source, SourceRevision, EvidenceChunk
from services.worker import main as worker


@pytest.fixture()
def local_library(tmp_path, monkeypatch):
    paths = local_paths(tmp_path / 'data', tmp_path / 'cache', tmp_path / 'logs')
    paths.prepare()
    engine = make_engine(f'sqlite:///{paths.database}')
    migrate(engine, paths.backups)
    identity = initialize_identity(engine, 'j' * 48)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sessions() as db:
        domain = Domain(owner_id=identity['owner_id'], name='合成领域')
        project = Project(owner_id=identity['owner_id'], name='合成项目')
        db.add_all([domain, project])
        db.commit()
        identity.update(domain_id=domain.id, project_id=project.id)
    for module in (auth, files, local_trash, main, services, worker):
        monkeypatch.setattr(module, 'settings', replace(
            module.settings, environment='local', database_url=f'sqlite:///{paths.database}',
            upload_dir=paths.assets, local_data_dir=str(paths.data),
            local_cache_dir=str(paths.cache), local_log_dir=str(paths.logs)))
    monkeypatch.setattr(worker, 'index_one', lambda _: False)
    previous = main.app.dependency_overrides.get(get_db)

    def database():
        with sessions() as db:
            yield db

    main.app.dependency_overrides[get_db] = database
    try:
        with TestClient(main.app) as client:
            yield paths, engine, sessions, identity, client
    finally:
        if previous is None:
            main.app.dependency_overrides.pop(get_db, None)
        else:
            main.app.dependency_overrides[get_db] = previous
        engine.dispose()


def capture_note(library, key='journal-note'):
    _, _, _, identity, client = library
    response = client.post('/v1/captures',
                           headers={'Authorization': 'Bearer ' + 'j' * 48, 'Idempotency-Key': key},
                           json={'capture_type': 'text', 'title': key, 'coverage': 'full_text',
                                 'content': {'text': '合成正文'},
                                 'context': {'domain_id': identity['domain_id'],
                                             'project_id': identity['project_id'], 'note': '合成补充'},
                                 'client': {'kind': 'desktop', 'version': 'journal-test'}})
    assert response.status_code == 201, response.text
    return response.json()


def state(db, kind, identifier):
    from app.models import SyncEntityState
    return db.get(SyncEntityState, (kind, identifier))


def changes(db, identifier=None):
    from app.models import LocalChange
    statement = select(LocalChange).order_by(LocalChange.sequence)
    if identifier:
        statement = statement.where(LocalChange.entity_id == identifier)
    return db.scalars(statement).all()


def test_capture_and_supplement_journal_same_transaction(local_library):
    _, _, sessions, identity, http = local_library
    saved = capture_note(local_library)
    with sessions() as db:
        original = state(db, 'source', saved['id']).generation
        assert original > 0
        assert all(row.device_id == identity['device_id'] for row in changes(db))
        assert state(db, 'source', saved['id']).acknowledged_generation == 0
    response = http.post(f'/v1/sources/{saved["id"]}/supplements',
                         headers={'Authorization': 'Bearer ' + 'j' * 48}, json={'text': '第二版本补充'})
    assert response.status_code == 201, response.text
    with sessions() as db:
        assert state(db, 'source', saved['id']).generation > original
        assert all(row.entity_kind == 'source' for row in changes(db, saved['id']))
        assert db.scalars(select(SourceRevision).where(SourceRevision.source_id == saved['id'])).all()


def test_bulk_updates_and_rollback_do_not_lose_or_leak_changes(local_library):
    _, _, sessions, _, _ = local_library
    first = capture_note(local_library, 'bulk-first')
    second = capture_note(local_library, 'bulk-second')
    with sessions() as db:
        before = {saved['id']: state(db, 'source', saved['id']).generation for saved in (first, second)}
        count = len(changes(db))
        db.execute(update(Source).where(Source.id.in_(before)).values(title='将回滚'))
        assert len(changes(db)) == count + 2
        db.rollback()
        assert len(changes(db)) == count
        assert db.get(Source, first['id']).title == 'bulk-first'
        assert state(db, 'source', first['id']).generation == before[first['id']]
        db.execute(update(Source).where(Source.id.in_(before)).values(title='批量生效'))
        db.commit()
    with sessions() as db:
        for identifier, generation in before.items():
            assert state(db, 'source', identifier).generation == generation + 1


def test_worker_revision_and_evidence_are_recorded(local_library):
    _, _, sessions, identity, http = local_library
    response = http.post('/v1/file-captures',
                         params={'domain_id': identity['domain_id'], 'filename': 'journal.csv'},
                         content=b'name,value\nsynthetic,1\n',
                         headers={'Authorization': 'Bearer ' + 'j' * 48, 'Idempotency-Key': 'journal-file',
                                  'Content-Type': 'application/octet-stream'})
    assert response.status_code == 201, response.text
    identifier = response.json()['id']
    with sessions() as db:
        before = state(db, 'source', identifier).generation
        assert worker.run_document_once(db, lambda *_: 'Worker 合成解析正文')
        db.expire_all()
        assert state(db, 'source', identifier).generation > before
        source = db.get(Source, identifier)
        assert db.get(SourceRevision, source.current_revision_id).extracted_text == 'Worker 合成解析正文'
        assert db.scalar(select(EvidenceChunk).where(EvidenceChunk.revision_id == source.current_revision_id)) is not None


def test_auth_index_and_job_progress_are_not_business_changes(local_library):
    from app.models import (AccountToken, AuditEvent, EvidenceEmbedding, SemanticIndexJob,
                            SourceSearchDocument, SourceSearchTerm, Session as UserSession)
    _, engine, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    with sessions() as db:
        before = len(changes(db))
        generation = state(db, 'source', saved['id']).generation
        source = db.get(Source, saved['id'])
        evidence = db.scalar(select(EvidenceChunk).where(EvidenceChunk.revision_id == source.current_revision_id))
        db.add(UserSession(user_id=identity['owner_id'], token_hash='f' * 64,
                           expires_at=datetime.now(timezone.utc)))
        db.add(AccountToken(user_id=identity['owner_id'], purpose='reset', token_hash='g' * 64,
                            expires_at=datetime.now(timezone.utc)))
        db.add(AuditEvent(owner_id=identity['owner_id'], tool='synthetic', result='ok'))
        db.add(EvidenceEmbedding(owner_id=identity['owner_id'], source_id=source.id,
                                revision_id=source.current_revision_id, evidence_id=evidence.id,
                                start=0, end=4, model='synthetic', vector=b'local-index'))
        document_id = db.scalar(select(SourceSearchDocument.id).where(SourceSearchDocument.source_id == source.id))
        db.add(SourceSearchTerm(document_id=document_id, term='synthetic-index'))
        db.execute(update(SemanticIndexJob).where(SemanticIndexJob.source_id == source.id).values(status='completed'))
        source.processing_status = 'pending'
        db.commit()
    initialize_identity(engine, 'k' * 48)
    with sessions() as db:
        assert len(changes(db)) == before
        assert state(db, 'source', saved['id']).generation == generation


def test_soft_delete_restore_and_purge_keep_entity_state(local_library):
    _, _, sessions, _, http = local_library
    saved = capture_note(local_library)
    headers = {'Authorization': 'Bearer ' + 'j' * 48}
    assert http.delete(f'/v1/sources/{saved["id"]}', headers=headers).status_code == 202
    with sessions() as db:
        deleted_generation = state(db, 'source', saved['id']).generation
    assert http.post(f'/v1/trash/{saved["id"]}/restore', headers=headers).status_code == 200
    with sessions() as db:
        assert state(db, 'source', saved['id']).generation > deleted_generation
    assert http.delete(f'/v1/sources/{saved["id"]}', headers=headers).status_code == 202
    assert http.delete(f'/v1/trash/{saved["id"]}', headers=headers).status_code == 202
    with sessions() as db:
        assert worker.run_once(db)
        db.expire_all()
        assert db.get(Source, saved['id']).processing_status == 'deleted'
        assert state(db, 'source', saved['id']) is not None
        assert changes(db, saved['id'])[-1].entity_kind == 'source'


def test_journal_initialization_is_idempotent_and_identity_bound(local_library):
    from app.local_changes import initialize_journal
    _, engine, sessions, identity, _ = local_library
    capture_note(local_library)
    with sessions() as db:
        before = len(changes(db))
    with engine.begin() as connection:
        initialize_journal(connection, identity['library_id'], identity['device_id'])
    with sessions() as db:
        assert len(changes(db)) == before
    with pytest.raises(ValueError):
        with engine.begin() as connection:
            initialize_journal(connection, identity['library_id'], 'different-device')


def test_hard_deleted_entity_keeps_delete_change(local_library):
    _, _, sessions, identity, _ = local_library
    with sessions() as db:
        source = Source(owner_id=identity['owner_id'], domain_id=identity['domain_id'],
                        kind='text', title='孤立合成资料', coverage='full_text')
        db.add(source)
        db.commit()
        identifier = source.id
        db.execute(delete(Source).where(Source.id == identifier))
        db.commit()
    with sessions() as db:
        assert db.get(Source, identifier) is None
        assert state(db, 'source', identifier).generation == 2
        assert changes(db, identifier)[-1].change_type == 'delete'


def test_v1_upgrade_records_existing_baseline_without_rewriting_knowledge(local_library):
    from app.local_changes import EXPECTED_TRIGGER_SQL, SYNC_TABLES
    _, engine, sessions, identity, _ = local_library
    saved = capture_note(local_library)
    with sessions() as db:
        original_revision = db.get(Source, saved['id']).current_revision_id
        original_evidence = db.scalar(select(EvidenceChunk.id).where(EvidenceChunk.revision_id == original_revision))
    with engine.begin() as connection:
        for name in EXPECTED_TRIGGER_SQL:
            connection.exec_driver_sql(f'DROP TRIGGER {name}')
        for table in SYNC_TABLES:
            connection.exec_driver_sql(f'DROP TABLE {table}')
        for table in ('rag_jobs', 'rag_turns', 'rag_sessions', 'summary_search_terms', 'summary_embeddings', 'summary_index_jobs', 'ai_job_steps', 'ai_jobs', 'ai_artifacts'):
            connection.exec_driver_sql(f'DROP TABLE {table}')
        for table in ('tag_suggestions', 'tag_rejections', 'tag_assignments', 'knowledge_tag_aliases', 'knowledge_tags'):
            connection.exec_driver_sql(f'DROP TABLE {table}')
        connection.exec_driver_sql('DELETE FROM schema_migrations WHERE version >= 2')
    paths = local_library[0]
    assert migrate(engine, paths.backups) is not None
    with sessions() as db:
        assert db.get(Source, saved['id']).current_revision_id == original_revision
        assert db.get(EvidenceChunk, original_evidence) is not None
        baseline = changes(db)
        assert {(row.entity_kind, row.entity_id) for row in baseline} == {
            ('domain', identity['domain_id']), ('project', identity['project_id']), ('source', saved['id'])}
        assert all(row.generation == 1 and row.device_id == identity['device_id'] for row in baseline)
    assert migrate(engine, paths.backups) is None


def test_other_owner_is_excluded_and_reassigned_context_records_both_sources(local_library):
    from app.models import User
    _, _, sessions, identity, _ = local_library
    first = capture_note(local_library, 'context-first')
    second = capture_note(local_library, 'context-second')
    with sessions() as db:
        count = len(changes(db))
        owner = User(email='journal-other@example.invalid', name='另一合成账号')
        db.add(owner)
        db.flush()
        domain = Domain(owner_id=owner.id, name='非绑定领域')
        db.add(domain)
        db.flush()
        source = Source(owner_id=owner.id, domain_id=domain.id, kind='text', title='非绑定资料', coverage='full_text')
        db.add(source)
        db.commit()
        assert len(changes(db)) == count
        assert state(db, 'domain', domain.id) is None and state(db, 'source', source.id) is None
        first_before = state(db, 'source', first['id']).generation
        second_before = state(db, 'source', second['id']).generation
        db.execute(delete(PersonalContext).where(PersonalContext.source_id == second['id']))
        db.commit()
        second_after_delete = state(db, 'source', second['id']).generation
        assert second_after_delete == second_before + 1
        db.execute(update(PersonalContext).where(PersonalContext.source_id == first['id']).values(source_id=second['id']))
        db.commit()
        assert state(db, 'source', first['id']).generation == first_before + 1
        assert state(db, 'source', second['id']).generation == second_after_delete + 1


def test_missing_or_changed_trigger_prevents_local_start(local_library):
    from app.migrations import require_schema
    _, engine, _, _, _ = local_library
    with engine.begin() as connection:
        connection.exec_driver_sql('DROP TRIGGER pka_change_sources_update')
    with engine.connect() as connection:
        with pytest.raises(ValueError, match='触发器'):
            require_schema(connection)
