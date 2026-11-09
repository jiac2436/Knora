"""Document retry contract, isolated original files, and concurrent submissions."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
from threading import Barrier

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app import files
from app.database import engine, get_db, make_engine
from app.main import app
from app.models import DocumentJob, FileAsset, Source, SourceRevision, utcnow
from services.worker import main as worker


@pytest.fixture()
def document_storage(client, account, tmp_path, monkeypatch):
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

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = get_session
    assets = tmp_path / 'assets'
    monkeypatch.setattr(files, 'settings', replace(files.settings, upload_dir=assets))
    monkeypatch.setattr(worker, 'settings', replace(worker.settings, upload_dir=assets))
    try:
        with TestClient(app) as http:
            yield http, sessions, assets
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous
        database.dispose()


def upload(http, account, filename='synthetic.csv', body=b'name,value\nsynthetic,1\n'):
    response = http.post('/v1/file-captures',
                         params={'domain_id': account['domain']['id'], 'filename': filename},
                         content=body, headers={**account['headers'], 'Idempotency-Key': filename,
                                                'Content-Type': 'application/octet-stream'})
    assert response.status_code == 201, response.text
    return response.json()


def fail_document(sessions, source_id):
    with sessions() as session:
        job = session.scalar(select(DocumentJob).where(DocumentJob.source_id == source_id))
        job.status = 'failed'
        job.attempts = 2
        job.last_error = '受控解析失败'
        job.completed_at = utcnow()
        session.get(Source, source_id).processing_status = 'failed'
        session.commit()
        return job.id


def retry(http, account, source_id):
    return http.post(f'/v1/sources/{source_id}/document-parse', headers=account['headers'])


def test_failed_document_retry_preserves_versions_supplement_and_original(document_storage, account):
    http, sessions, _ = document_storage
    source = upload(http, account)
    source_id = source['id']
    with sessions() as session:
        assert worker.run_document_once(session, lambda *_: '首次提取的正文')
    assert http.post(f'/v1/sources/{source_id}/supplements', headers=account['headers'],
                     json={'text': '用户自己的补充'}).status_code == 201
    with sessions() as session:
        job = session.scalar(select(DocumentJob).where(DocumentJob.source_id == source_id))
        job.status = 'pending'
        session.commit()

        def broken_document(*_):
            raise ValueError('受控解析失败')

        assert worker.run_document_once(session, broken_document)
        job_id, attempts = job.id, job.attempts
        current_revision = session.get(Source, source_id).current_revision_id
        revisions = [(row.id, row.version, row.extracted_text) for row in session.scalars(
            select(SourceRevision).where(SourceRevision.source_id == source_id)
            .order_by(SourceRevision.version))]
    response = retry(http, account, source_id)
    assert response.status_code == 202, response.text
    assert response.json() == {'id': job_id, 'source_id': source_id, 'status': 'pending'}
    detail = http.get(f'/v1/sources/{source_id}', headers=account['headers']).json()
    assert detail['extracted_text'] == '首次提取的正文\n\n用户自己的补充'
    assert detail['processing_status'] == 'pending'
    assert http.get(source['file']['download_path'], headers=account['headers']).content == b'name,value\nsynthetic,1\n'
    with sessions() as session:
        assert session.get(Source, source_id).current_revision_id == current_revision
        assert [(row.id, row.version, row.extracted_text) for row in session.scalars(
            select(SourceRevision).where(SourceRevision.source_id == source_id)
            .order_by(SourceRevision.version))] == revisions
        job = session.get(DocumentJob, job_id)
        assert job.attempts == attempts and job.last_error is None and job.completed_at is None
        assert session.scalar(select(func.count()).select_from(DocumentJob)
                              .where(DocumentJob.source_id == source_id)) == 1
    assert retry(http, account, source_id).status_code == 409
    with sessions() as session:
        assert worker.run_document_once(session, lambda *_: '恢复后提取的正文')
        job = session.get(DocumentJob, job_id)
        assert job.status == 'completed' and job.attempts == attempts + 1
    recovered = http.get(f'/v1/sources/{source_id}', headers=account['headers']).json()
    assert recovered['extracted_text'] == '恢复后提取的正文\n\n用户自己的补充'
    assert retry(http, account, source_id).status_code == 409


@pytest.mark.parametrize('status', ['pending', 'running', 'completed', 'cancelled'])
def test_only_failed_document_jobs_can_retry(document_storage, account, status):
    http, sessions, _ = document_storage
    source = upload(http, account)
    with sessions() as session:
        job = session.scalar(select(DocumentJob).where(DocumentJob.source_id == source['id']))
        job.status = status
        session.commit()
        job_id = job.id
    response = retry(http, account, source['id'])
    assert response.status_code == 409
    expected = 'document_parse_in_progress' if status in {'pending', 'running'} else 'document_parse_not_failed'
    assert response.json()['code'] == expected
    with sessions() as session:
        assert session.get(DocumentJob, job_id).status == status


def test_document_retry_owner_deleted_and_unknown_isolation(document_storage, account):
    http, sessions, _ = document_storage
    source = upload(http, account)
    job_id = fail_document(sessions, source['id'])
    path = f'/v1/sources/{source["id"]}/document-parse'
    assert http.post(path).status_code == 401
    other = http.post('/v1/dev/sessions', json={'email': 'other-retry@example.com', 'name': 'Other'}).json()
    assert http.post(path, headers={'Authorization': f'Bearer {other["access_token"]}'}).status_code == 404
    assert retry(http, account, 'unknown-source').status_code == 404
    with sessions() as session:
        session.get(Source, source['id']).deleted_at = utcnow()
        session.commit()
    assert retry(http, account, source['id']).status_code == 404
    with sessions() as session:
        assert session.get(DocumentJob, job_id).status == 'failed'


def test_missing_document_original_does_not_requeue(document_storage, account):
    http, sessions, assets = document_storage
    source = upload(http, account)
    job_id = fail_document(sessions, source['id'])
    with sessions() as session:
        asset = session.scalar(select(FileAsset).where(FileAsset.source_id == source['id']))
        (assets / asset.id).unlink()
    response = retry(http, account, source['id'])
    assert response.status_code == 404 and response.json()['code'] == 'original_missing'
    with sessions() as session:
        job = session.get(DocumentJob, job_id)
        assert job.status == 'failed' and job.last_error == '受控解析失败'


@pytest.mark.parametrize('filename', ['synthetic.pdf', 'disguised.csv'])
def test_pdf_and_pdf_disguised_as_document_cannot_document_retry(document_storage, account, filename):
    http, _, _ = document_storage
    source = upload(http, account, filename, b'%PDF-synthetic')
    response = retry(http, account, source['id'])
    assert response.status_code == 422 and response.json()['code'] == 'not_document'


def test_note_cannot_document_retry(document_storage, account):
    http, _, _ = document_storage
    response = http.post('/v1/captures', headers={**account['headers'], 'Idempotency-Key': 'note'},
                         json={'capture_type': 'text', 'title': '笔记', 'coverage': 'full_text',
                               'content': {'text': '合成笔记'},
                               'context': {'domain_id': account['domain']['id']},
                               'client': {'kind': 'desktop', 'version': 'retry-test'}})
    assert response.status_code == 201
    response = retry(http, account, response.json()['id'])
    assert response.status_code == 422 and response.json()['code'] == 'not_document'


def test_concurrent_retry_accepts_one_request_and_reuses_job(document_storage, account):
    http, sessions, _ = document_storage
    source = upload(http, account)
    job_id = fail_document(sessions, source['id'])
    start = Barrier(2)

    def submit():
        start.wait(timeout=5)
        return retry(http, account, source['id'])

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(response.status_code for response in responses) == [202, 409]
    assert next(response for response in responses if response.status_code == 202).json()['id'] == job_id
    with sessions() as session:
        jobs = session.scalars(select(DocumentJob).where(DocumentJob.source_id == source['id'])).all()
        assert len(jobs) == 1 and jobs[0].id == job_id and jobs[0].status == 'pending'
        assert jobs[0].attempts == 2


@pytest.mark.parametrize('active_status', ['pending', 'running'])
def test_active_job_blocks_retry_of_failed_job_for_same_source(document_storage, account, active_status):
    http, sessions, _ = document_storage
    source = upload(http, account)
    failed_id = fail_document(sessions, source['id'])
    with sessions() as session:
        session.add(DocumentJob(source_id=source['id'], owner_id=account['user']['id'],
                                status=active_status))
        session.commit()
    response = retry(http, account, source['id'])
    assert response.status_code == 409 and response.json()['code'] == 'document_parse_in_progress'
    with sessions() as session:
        assert session.get(DocumentJob, failed_id).status == 'failed'
        assert session.scalar(select(func.count()).select_from(DocumentJob)
                              .where(DocumentJob.source_id == source['id'])) == 2


def test_document_without_job_does_not_create_retry_job(document_storage, account):
    http, sessions, _ = document_storage
    source = upload(http, account)
    with sessions() as session:
        job = session.scalar(select(DocumentJob).where(DocumentJob.source_id == source['id']))
        session.delete(job)
        session.commit()
    response = retry(http, account, source['id'])
    assert response.status_code == 409 and response.json()['code'] == 'document_parse_not_failed'
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(DocumentJob)
                              .where(DocumentJob.source_id == source['id'])) == 0
