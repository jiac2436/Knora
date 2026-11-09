"""Local trash retention, isolated restoration, purge and worker race contracts."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import sqlite3
import sys
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from app import files, main, services
from app.database import engine, get_db, make_engine
from app.main import app
from app.models import (DeletionJob, DocumentJob, OCRJob, WebReadJob, EvidenceChunk, FileAsset,
                        SemanticIndexJob, Source, SourceRevision, SourceImage, SourceTag, SourceTagState)
from services.worker import main as worker


def test_permanent_delete_clears_capture_execution_originals(trash_storage, account, monkeypatch):
    from app import capture_handoff, capture_queue
    from app.capture_store import CaptureStore
    from services.worker import capture as capture_worker
    http, sessions, _, _ = trash_storage
    journal = CaptureStore(worker.settings.local_paths.data)
    monkeypatch.setattr(capture_queue, 'store', lambda: journal)
    monkeypatch.setattr(capture_handoff, 'settings', worker.settings)
    monkeypatch.setattr(capture_worker, 'settings', worker.settings)
    identifier = str(uuid4())
    key = str(uuid4())
    raw = b'synthetic,137\n'
    journal.files.atomic_write(journal.client_files / key, raw)
    value = {'id': identifier, 'kind': 'file', 'title': 'synthetic.csv',
             'domain_id': account['domain']['id'], 'file': {'key': key, 'sha256': hashlib.sha256(raw).hexdigest()}}
    submitted = http.post('/v1/capture-queue', headers=account['headers'], json=value)
    assert submitted.status_code == 202, submitted.text
    with sessions() as session:
        assert capture_worker.run_capture_once(session, journal)
    record = journal.load(identifier)
    assert record['state'] == 'completed' and journal.work(identifier).exists()
    soft_delete(http, account, record['source_id'])
    assert purge(http, account, record['source_id']).status_code == 202
    with sessions() as session:
        assert worker.run_once(session)
        assert session.get(Source, record['source_id']).processing_status == 'deleted'
    after = journal.load(identifier)
    assert not journal.work(identifier).exists() and not after['assets']
    assert after['input']['file'] is None and after['state'] == 'completed'
    assert http.post('/v1/capture-queue', headers=account['headers'], json=value).status_code in {404, 409}


@pytest.fixture()
def trash_storage(client, account, tmp_path, monkeypatch):
    other = client.post('/v1/dev/sessions', json={'email': 'trash-other@example.com', 'name': 'Other'}).json()
    other_headers = {'Authorization': f'Bearer {other["access_token"]}'}
    target = sqlite3.connect(tmp_path / 'knowledge.db')
    original = engine.raw_connection()
    try:
        original.driver_connection.backup(target)
    finally:
        original.close()
        target.close()
    database = make_engine(f'sqlite:///{tmp_path / "knowledge.db"}')
    sessions = sessionmaker(bind=database, autoflush=False, expire_on_commit=False)

    def get_session():
        with sessions() as session:
            yield session

    assets = tmp_path / 'assets'
    from app import local_trash
    for module in (main, services, files, local_trash, worker):
        monkeypatch.setattr(module, 'settings', replace(
            module.settings, environment='local', upload_dir=assets,
            database_url=f'sqlite:///{tmp_path / "knowledge.db"}', local_data_dir=str(tmp_path),
            local_cache_dir=str(tmp_path / 'cache'), local_log_dir=str(tmp_path / 'logs')))
    monkeypatch.setattr(worker, 'index_one', lambda _: False)
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = get_session
    try:
        with TestClient(app) as http:
            yield http, sessions, assets, other_headers
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous
        database.dispose()


def note(http, account, key='trash-note'):
    response = http.post('/v1/captures', headers={**account['headers'], 'Idempotency-Key': key},
                         json={'capture_type': 'text', 'title': key, 'coverage': 'full_text',
                               'content': {'text': '回收站原始正文'},
                               'context': {'domain_id': account['domain']['id']},
                               'client': {'kind': 'desktop', 'version': 'trash-test'}})
    assert response.status_code == 201, response.text
    return response.json()


def document(http, account):
    body = b'name,value\nsynthetic,1\n'
    response = http.post('/v1/file-captures', params={'domain_id': account['domain']['id'], 'filename': 'trash.csv'},
                         content=body, headers={**account['headers'], 'Idempotency-Key': 'trash-file',
                                                'Content-Type': 'application/octet-stream'})
    assert response.status_code == 201, response.text
    return response.json(), body


def soft_delete(http, account, source_id):
    response = http.delete(f'/v1/sources/{source_id}', headers=account['headers'])
    assert response.status_code == 202, response.text
    assert response.json()['status'] == 'trashed'
    return response.json()['operation_id']


def restore(http, account, source_id):
    return http.post(f'/v1/trash/{source_id}/restore', headers=account['headers'])


def purge(http, account, source_id):
    return http.delete(f'/v1/trash/{source_id}', headers=account['headers'])


def test_deleted_note_hidden_from_sources_search_and_historical_evidence(trash_storage, account):
    http, sessions, _, _ = trash_storage
    saved = note(http, account)
    old_evidence = saved['evidence'][0]['evidence_id']
    assert http.post(f'/v1/sources/{saved["id"]}/supplements', headers=account['headers'],
                     json={'text': '用户补充保留'}).status_code == 201
    with sessions() as session:
        revisions = [(row.id, row.extracted_text) for row in session.scalars(
            select(SourceRevision).where(SourceRevision.source_id == saved['id']).order_by(SourceRevision.version))]
    job_id = soft_delete(http, account, saved['id'])
    assert http.get(f'/v1/sources/{saved["id"]}', headers=account['headers']).status_code == 404
    assert http.get(f'/v1/evidence/{old_evidence}', headers=account['headers']).status_code == 404
    assert http.get(f'/v1/sources/{saved["id"]}/revisions', headers=account['headers']).status_code == 404
    assert http.post('/v1/search', headers=account['headers'], json={'query': '回收站'}).json()['items'] == []
    assert saved['id'] not in [item['id'] for item in http.get('/v1/sources', headers=account['headers']).json()['items']]
    with sessions() as session:
        # A trashed source can invalidate local topic directories. Processing
        # that local work must settle without physically purging the source.
        processed = 0
        while worker.run_once(session):
            processed += 1
            assert processed < 20
        assert not worker.run_once(session)
        assert session.get(DeletionJob, job_id).status == 'trashed'
        assert [(row.id, row.extracted_text) for row in session.scalars(
            select(SourceRevision).where(SourceRevision.source_id == saved['id']).order_by(SourceRevision.version))] == revisions
    restored = restore(http, account, saved['id'])
    assert restored.status_code == 200 and restored.json() == {'id': saved['id'], 'status': 'restored'}
    assert http.get(f'/v1/evidence/{old_evidence}', headers=account['headers']).status_code == 200
    assert http.get(f'/v1/sources/{saved["id"]}', headers=account['headers']).json()['extracted_text'].endswith('用户补充保留')
    with sessions() as session:
        assert session.get(DeletionJob, job_id).status == 'cancelled'
        source = session.get(Source, saved['id'])
        indexing = session.get(SemanticIndexJob, saved['id'])
        assert indexing.status == 'pending' and indexing.revision_id == source.current_revision_id
    assert restore(http, account, saved['id']).status_code in {404, 409}


def test_document_restore_keeps_original_history_and_cancelled_job_does_not_replay(trash_storage, account):
    http, sessions, assets, _ = trash_storage
    saved, body = document(http, account)
    with sessions() as session:
        assert worker.run_document_once(session, lambda *_: '已经解析的正文')
    assert http.post(f'/v1/sources/{saved["id"]}/supplements', headers=account['headers'],
                     json={'text': '解析之外的用户补充'}).status_code == 201
    with sessions() as session:
        current = session.get(Source, saved['id']).current_revision_id
        job = session.scalar(select(DocumentJob).where(DocumentJob.source_id == saved['id']))
        job.status = 'pending'
        session.commit()
        job_id = job.id
        asset_id = session.scalar(select(FileAsset.id).where(FileAsset.source_id == saved['id']))
    soft_delete(http, account, saved['id'])
    assert (assets / asset_id).read_bytes() == body
    assert http.get(saved['file']['download_path'], headers=account['headers']).status_code == 404
    assert restore(http, account, saved['id']).status_code == 200
    assert http.get(saved['file']['download_path'], headers=account['headers']).content == body
    with sessions() as session:
        assert session.get(DocumentJob, job_id).status == 'failed'
        assert not worker.run_document_once(session, lambda *_: pytest.fail('恢复不应自动重新解析'))
        assert session.get(Source, saved['id']).current_revision_id == current
        assert session.scalar(select(func.count()).select_from(SourceRevision).where(SourceRevision.source_id == saved['id'])) == 2


def test_running_document_old_result_cannot_overwrite_delete_then_restore(trash_storage, account):
    http, sessions, _, _ = trash_storage
    saved, _ = document(http, account)
    assert http.post(f'/v1/sources/{saved["id"]}/supplements', headers=account['headers'],
                     json={'text': '尚未解析时的用户补充'}).status_code == 201
    with sessions() as session:
        current = session.get(Source, saved['id']).current_revision_id

        def extract(*_):
            soft_delete(http, account, saved['id'])
            assert restore(http, account, saved['id']).status_code == 200
            return '旧任务不允许回写的正文'

        assert worker.run_document_once(session, extract)
        session.expire_all()
        assert session.get(Source, saved['id']).current_revision_id == current
        assert session.get(SourceRevision, current).extracted_text == '尚未解析时的用户补充'
        assert session.scalar(select(DocumentJob).where(DocumentJob.source_id == saved['id'])).status == 'failed'


def processing_case(http, sessions, account, kind):
    saved, _ = document(http, account)
    assert http.post(f'/v1/sources/{saved["id"]}/supplements', headers=account['headers'],
                     json={'text': '领取竞态中的用户补充'}).status_code == 201
    model, runner = {'document': (DocumentJob, worker.run_document_once),
                     'ocr': (OCRJob, worker.run_ocr_once),
                     'web': (WebReadJob, worker.run_web_once)}[kind]
    with sessions() as session:
        if model is not DocumentJob:
            session.execute(delete(DocumentJob).where(DocumentJob.source_id == saved['id']))
            options = {'model': 'small'} if model is OCRJob else {}
            session.add(model(source_id=saved['id'], owner_id=account['user']['id'], status='pending', **options))
            if model is WebReadJob:
                session.get(Source, saved['id']).raw_url = 'https://example.com/synthetic-trash-race'
            session.commit()
        current = session.get(Source, saved['id']).current_revision_id
        job_id = session.scalar(select(model.id).where(model.source_id == saved['id']))
    return saved, current, job_id, model, runner


@pytest.mark.parametrize('kind', ['document', 'ocr', 'web'])
def test_initial_claim_rechecks_job_after_pending_read_delete_and_restore(trash_storage, account, monkeypatch, kind):
    http, sessions, _, _ = trash_storage
    saved, current, job_id, model, runner = processing_case(http, sessions, account, kind)
    with sessions() as session:
        original_scalar = session.scalar
        injected = []

        def read_then_cancel(statement, *args, **kwargs):
            value = original_scalar(statement, *args, **kwargs)
            if isinstance(value, model) and not injected:
                injected.append(True)
                # The reader holds a stale pending ORM object; the other connection commits cancellation.
                soft_delete(http, account, saved['id'])
                assert restore(http, account, saved['id']).status_code == 200
            return value

        monkeypatch.setattr(session, 'scalar', read_then_cancel)
        assert runner(session, lambda *_: pytest.fail('已取消的旧任务不能领取或调用解析器'))
        assert injected == [True]
        session.expire_all()
        job = session.get(model, job_id)
        assert job.status == 'failed' and job.attempts == 0
        source = session.get(Source, saved['id'])
        assert source.current_revision_id == current and source.processing_status == 'usable'
        assert session.get(SourceRevision, current).extracted_text == '领取竞态中的用户补充'


@pytest.mark.parametrize('kind', ['document', 'ocr', 'web'])
def test_exception_publication_rejects_cancelled_restored_old_job(trash_storage, account, kind):
    http, sessions, _, _ = trash_storage
    saved, current, job_id, model, runner = processing_case(http, sessions, account, kind)
    with sessions() as session:

        def old_failure(*_):
            soft_delete(http, account, saved['id'])
            assert restore(http, account, saved['id']).status_code == 200
            raise ValueError('旧解析器失败信息不得覆盖取消记录')

        assert runner(session, old_failure)
        session.expire_all()
        job = session.get(model, job_id)
        assert job.status == 'failed' and job.attempts == 1
        assert job.last_error == '资料曾移入回收站，请手动重试处理。'
        source = session.get(Source, saved['id'])
        assert source.current_revision_id == current and source.processing_status == 'usable'
        assert session.get(SourceRevision, current).extracted_text == '领取竞态中的用户补充'


def test_trash_owner_authentication_and_active_source_isolation(trash_storage, account):
    http, _, _, other = trash_storage
    saved = note(http, account)
    assert restore(http, account, saved['id']).status_code in {404, 409}
    assert purge(http, account, saved['id']).status_code in {404, 409}
    soft_delete(http, account, saved['id'])
    assert http.get('/v1/trash').status_code == 401
    assert http.get('/v1/trash', headers=other).json()['items'] == []
    for method, path in [('post', f'/v1/trash/{saved["id"]}/restore'), ('delete', f'/v1/trash/{saved["id"]}')]:
        assert getattr(http, method)(path, headers=other).status_code == 404
        assert getattr(http, method)(path).status_code == 401
    assert restore(http, account, 'unknown').status_code == 404
    assert purge(http, account, 'unknown').status_code == 404


def test_trash_pagination_and_restore_state(trash_storage, account):
    http, _, _, _ = trash_storage
    ids = [note(http, account, f'trash-{number}')['id'] for number in range(3)]
    for source_id in ids:
        soft_delete(http, account, source_id)
    first = http.get('/v1/trash?limit=2&offset=0', headers=account['headers']).json()
    second = http.get('/v1/trash?limit=2&offset=2', headers=account['headers']).json()
    assert first['next_offset'] == 2 and second['next_offset'] is None
    assert {item['id'] for item in first['items'] + second['items']} == set(ids)
    assert all(item['can_restore'] and item['deleted_at'] and item['content_type'] == 'note'
               for item in first['items'] + second['items'])
    assert http.get('/v1/trash?limit=0', headers=account['headers']).status_code == 422
    assert http.get('/v1/trash?offset=-1', headers=account['headers']).status_code == 422


@pytest.mark.parametrize('damage', ['missing', 'changed', 'symlink', 'traversal'])
def test_restore_rejects_unsafe_or_missing_original_without_changing_trash(trash_storage, account, tmp_path, damage):
    http, sessions, assets, _ = trash_storage
    saved, body = document(http, account)
    job_id = soft_delete(http, account, saved['id'])
    with sessions() as session:
        asset = session.scalar(select(FileAsset).where(FileAsset.source_id == saved['id']))
        original = assets / asset.id
        if damage == 'missing':
            original.unlink()
        elif damage == 'changed':
            original.write_bytes(body.replace(b'1', b'2'))
        elif damage == 'symlink':
            outside = tmp_path / 'synthetic-outside.csv'
            outside.write_bytes(body)
            original.unlink()
            original.symlink_to(outside)
        else:
            original.rename(tmp_path / 'synthetic-outside.csv')
            asset.id = '../synthetic-outside.csv'
            session.commit()
    response = restore(http, account, saved['id'])
    assert response.status_code == 409 and response.json()['code'] == 'trash_asset_unavailable'
    item = http.get('/v1/trash', headers=account['headers']).json()['items'][0]
    assert item['id'] == saved['id']
    with sessions() as session:
        assert session.get(Source, saved['id']).deleted_at is not None
        assert session.get(DeletionJob, job_id).status == 'trashed'


def test_permanent_delete_reuses_worker_and_cannot_restore_after_purge(trash_storage, account):
    http, sessions, assets, _ = trash_storage
    saved, _ = document(http, account)
    with sessions() as session:
        assert worker.run_document_once(session, lambda *_: '永久删除合成正文')
        asset_id = session.scalar(select(FileAsset.id).where(FileAsset.source_id == saved['id']))
        revision_ids = list(session.scalars(select(SourceRevision.id).where(SourceRevision.source_id == saved['id'])))
    soft_delete(http, account, saved['id'])
    operation = purge(http, account, saved['id'])
    assert operation.status_code == 202 and operation.json()['status'] == 'pending'
    repeated = purge(http, account, saved['id'])
    assert repeated.status_code == 202 and repeated.json()['operation_id'] == operation.json()['operation_id']
    assert restore(http, account, saved['id']).status_code == 409
    with sessions() as session:
        assert worker.run_once(session)
        assert session.get(DeletionJob, operation.json()['operation_id']).status == 'completed'
        assert session.get(Source, saved['id']).processing_status == 'deleted'
        for model in (SourceRevision, FileAsset, DocumentJob, SemanticIndexJob):
            assert session.scalar(select(func.count()).select_from(model).where(model.source_id == saved['id'])) == 0
        assert session.scalar(select(func.count()).select_from(EvidenceChunk).where(EvidenceChunk.revision_id.in_(revision_ids))) == 0
    assert not (assets / asset_id).exists()
    assert restore(http, account, saved['id']).status_code == 404
    assert http.get('/v1/trash', headers=account['headers']).json()['items'] == []


def test_saved_image_survives_restore_and_purge_removes_image_and_derived_tags(trash_storage, account):
    http, sessions, assets, _ = trash_storage
    saved = note(http, account)
    image = assets / 'images' / 'synthetic-image'
    image.parent.mkdir(parents=True)
    image.write_bytes(b'synthetic image bytes')
    with sessions() as session:
        revision_id = session.get(Source, saved['id']).current_revision_id
        session.add(SourceImage(id='synthetic-image-row', source_id=saved['id'], revision_id=revision_id,
                                ordinal=0, status='saved', file_key=image.name,
                                size_bytes=image.stat().st_size, sha256=hashlib.sha256(image.read_bytes()).hexdigest()))
        session.merge(SourceTag(source_id=saved['id'], name='synthetic tag'))
        session.merge(SourceTagState(source_id=saved['id'], revision_id=revision_id, rule_version=1))
        session.commit()
    soft_delete(http, account, saved['id'])
    assert restore(http, account, saved['id']).status_code == 200
    assert image.read_bytes() == b'synthetic image bytes'
    with sessions() as session:
        assert session.get(SourceImage, 'synthetic-image-row') is not None
        # Tags are derived again during restoration; create a row to test purge cleanup.
        session.merge(SourceTag(source_id=saved['id'], name='synthetic tag'))
        session.commit()
    soft_delete(http, account, saved['id'])
    assert purge(http, account, saved['id']).status_code == 202
    with sessions() as session:
        assert worker.run_once(session)
        assert session.get(SourceImage, 'synthetic-image-row') is None
        assert session.scalar(select(func.count()).select_from(SourceTag).where(SourceTag.source_id == saved['id'])) == 0
        assert session.get(SourceTagState, saved['id']) is None
    assert not image.exists()


@pytest.mark.parametrize('environment,processing,expected', [
    ('local', 'deleting', 'retry'), ('local', 'usable', 'running'), ('test', 'deleting', 'running')])
def test_worker_restart_only_resumes_local_confirmed_running_purge(
        trash_storage, account, monkeypatch, environment, processing, expected):
    http, sessions, _, _ = trash_storage
    saved = note(http, account)
    operation_id = soft_delete(http, account, saved['id'])
    with sessions() as session:
        session.get(Source, saved['id']).processing_status = processing
        session.get(DeletionJob, operation_id).status = 'running'
        session.commit()
    monkeypatch.setattr(worker, 'settings', replace(worker.settings, environment=environment))
    monkeypatch.setattr(worker, 'SessionLocal', sessions)
    monkeypatch.setattr(worker, 'create_schema', lambda: None)
    monkeypatch.setattr(worker, 'ensure_semantic_index', lambda _: None)
    monkeypatch.setattr(sys, 'argv', ['worker-test', '--once'])
    monkeypatch.delenv('PKA_WORKER_READY_FD', raising=False)
    received = []

    def inspect(session):
        received.append(session.get(DeletionJob, operation_id).status)
        return False

    real_run_once = worker.run_once
    monkeypatch.setattr(worker, 'run_once', inspect)
    worker.run_service()
    assert received == [expected]
    if expected == 'retry':
        with sessions() as session:
            assert real_run_once(session)
            assert session.get(DeletionJob, operation_id).status == 'completed'


def test_local_worker_ignores_unconfirmed_legacy_pending_cleanup(trash_storage, account):
    http, sessions, _, _ = trash_storage
    saved = note(http, account)
    operation_id = soft_delete(http, account, saved['id'])
    with sessions() as session:
        job = session.get(DeletionJob, operation_id)
        job.status = 'pending'
        session.commit()
        assert not worker.run_once(session)
        assert session.get(DeletionJob, operation_id).status == 'pending'
        assert session.get(Source, saved['id']).current_revision_id is not None


@pytest.mark.parametrize('damage', ['traversal', 'parent_symlink'])
def test_local_purge_never_removes_assets_outside_library(trash_storage, account, tmp_path, damage):
    http, sessions, assets, _ = trash_storage
    saved, body = document(http, account)
    outside = tmp_path / 'outside' / 'original.csv'
    outside.parent.mkdir()
    outside.write_bytes(body)
    with sessions() as session:
        asset = session.scalar(select(FileAsset).where(FileAsset.source_id == saved['id']))
        (assets / asset.id).unlink()
        if damage == 'traversal':
            asset.id = '../outside/original.csv'
        else:
            (assets / 'linked-dir').symlink_to(outside.parent, target_is_directory=True)
            asset.id = 'linked-dir/original.csv'
        session.commit()
    soft_delete(http, account, saved['id'])
    operation = purge(http, account, saved['id'])
    assert operation.status_code == 202
    with sessions() as session:
        assert not worker.run_once(session)
        assert session.get(DeletionJob, operation.json()['operation_id']).status == 'retry'
        assert session.scalar(select(FileAsset).where(FileAsset.source_id == saved['id'])) is not None
    assert outside.read_bytes() == body


def test_local_purge_rejects_linked_asset_root_without_removing_original(trash_storage, account, tmp_path):
    http, sessions, assets, _ = trash_storage
    saved, body = document(http, account)
    with sessions() as session:
        asset_id = session.scalar(select(FileAsset.id).where(FileAsset.source_id == saved['id']))
    soft_delete(http, account, saved['id'])
    moved_assets = tmp_path / 'synthetic-moved-assets'
    assets.rename(moved_assets)
    assets.symlink_to(moved_assets, target_is_directory=True)
    operation = purge(http, account, saved['id'])
    assert operation.status_code == 202
    with sessions() as session:
        assert not worker.run_once(session)
        assert session.get(DeletionJob, operation.json()['operation_id']).status == 'retry'
    assert (moved_assets / asset_id).read_bytes() == body


@pytest.mark.parametrize('action', ['restore', 'purge'])
def test_backup_restored_deleted_source_without_deletion_job_can_be_managed(trash_storage, account, action):
    http, sessions, _, _ = trash_storage
    saved = note(http, account)
    soft_delete(http, account, saved['id'])
    with sessions() as session:
        session.execute(delete(DeletionJob).where(DeletionJob.source_id == saved['id']))
        session.commit()
    listing = http.get('/v1/trash', headers=account['headers']).json()['items']
    assert listing[0]['id'] == saved['id'] and listing[0]['can_restore']
    response = restore(http, account, saved['id']) if action == 'restore' else purge(http, account, saved['id'])
    assert response.status_code == (200 if action == 'restore' else 202)


@pytest.mark.parametrize('actions', [('restore', 'restore'), ('restore', 'purge')])
def test_restore_and_purge_serialize_concurrent_changes(trash_storage, account, actions):
    http, sessions, _, _ = trash_storage
    saved = note(http, account)
    soft_delete(http, account, saved['id'])
    start = Barrier(2)

    def submit(action):
        start.wait(timeout=5)
        return action, (restore(http, account, saved['id']) if action == 'restore' else purge(http, account, saved['id']))

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, actions))
    assert sum(response.status_code in {200, 202} for _, response in responses) == 1
    assert all(response.status_code in {200, 202, 404, 409} for _, response in responses)
    with sessions() as session:
        source = session.get(Source, saved['id'])
        jobs = session.scalars(select(DeletionJob).where(DeletionJob.source_id == saved['id'])).all()
        if source.deleted_at is None:
            assert not any(job.status in {'pending', 'running', 'retry'} for job in jobs)
        else:
            assert sum(job.status in {'pending', 'running', 'retry'} for job in jobs) == 1


def test_nonlocal_delete_retains_old_pending_contract_and_trash_is_unavailable(client, account):
    saved = note(client, account)
    response = client.delete(f'/v1/sources/{saved["id"]}', headers=account['headers'])
    assert response.status_code == 202 and response.json()['status'] == 'pending'
    assert client.get('/v1/trash', headers=account['headers']).status_code == 404
    assert client.post(f'/v1/trash/{saved["id"]}/restore', headers=account['headers']).status_code == 404
    assert client.delete(f'/v1/trash/{saved["id"]}', headers=account['headers']).status_code == 404
