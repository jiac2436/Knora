"""Storage failure acceptance without filling the machine's filesystem."""
from dataclasses import replace
import errno
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import files, errors
from app.database import engine, get_db, make_engine
from app.main import app
from app.models import DocumentJob, Source
from services.worker import main as worker


@pytest.fixture()
def storage(client, account, tmp_path, monkeypatch):
    # Copy only the synthetic fixture DB into a real temporary SQLite file.
    target = sqlite3.connect(tmp_path / 'knowledge.db')
    source = engine.raw_connection()
    try:
        source.driver_connection.backup(target)
    finally:
        source.close()
        target.close()
    isolated = make_engine(f'sqlite:///{tmp_path / "knowledge.db"}')
    sessions = sessionmaker(bind=isolated, autoflush=False, expire_on_commit=False)

    def database():
        with sessions() as session:
            yield session

    previous = app.dependency_overrides.get(get_db)
    assets = tmp_path / 'assets'
    monkeypatch.setattr(files, 'settings', replace(files.settings, upload_dir=assets))
    monkeypatch.setattr(worker, 'settings', replace(worker.settings, upload_dir=assets))
    monkeypatch.setattr(errors, 'settings', SimpleNamespace(environment='local'))
    app.dependency_overrides[get_db] = database
    try:
        with TestClient(app, raise_server_exceptions=False) as http:
            yield http, isolated, assets
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous
        isolated.dispose()


def counts(database):
    tables = ('sources', 'source_revisions', 'evidence_chunks', 'personal_context',
              'idempotency_records', 'file_assets', 'document_jobs', 'semantic_index_jobs')
    with database.connect() as connection:
        return {table: connection.exec_driver_sql(f'SELECT COUNT(*) FROM {table}').scalar_one()
                for table in tables}


def note(http, account, key, text):
    return http.post('/v1/captures', headers={**account['headers'], 'Idempotency-Key': key},
                     json={'capture_type': 'text', 'title': '笔记', 'coverage': 'full_text',
                           'content': {'text': text}, 'context': {'domain_id': account['domain']['id']},
                           'client': {'kind': 'desktop', 'version': 'storage-test'}})


def upload(http, account, key):
    return http.post('/v1/file-captures', params={'domain_id': account['domain']['id'],
                                                'filename': 'synthetic.csv'},
                     content=b'name,value\nsynthetic,1\n',
                     headers={**account['headers'], 'Idempotency-Key': key,
                              'Content-Type': 'application/octet-stream'})


def test_sqlite_full_rolls_back_and_same_key_can_retry(storage, account):
    http, database, _ = storage
    original = note(http, account, 'existing-note', '已保存的合成资料').json()
    before = counts(database)
    # Prior tests can leave free pages in the fixture DB after dropping large tables.
    # Reclaim them only in this isolated copy so the page cap deterministically fills.
    with database.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
        connection.exec_driver_sql('VACUUM')
    with database.connect() as connection:
        page_limit = connection.exec_driver_sql('PRAGMA page_count').scalar_one()
        previous_limit = connection.exec_driver_sql('PRAGMA max_page_count').scalar_one()
    observed = []

    def constrain(connection, *_):
        connection.execute(f'PRAGMA max_page_count={page_limit}')

    def observe(context):
        observed.append(str(context.original_exception))

    event.listen(database, 'checkout', constrain)
    event.listen(database, 'handle_error', observe)
    try:
        failed = note(http, account, 'retry-note', '受控容量测试内容' * 8000)
        assert failed.status_code == 503
        assert failed.json()['code'] == 'local_storage_full' and failed.json()['retryable'] is True
        assert any('database or disk is full' in message for message in observed)
        assert counts(database) == before
        detail = http.get(f'/v1/sources/{original["id"]}', headers=account['headers'])
        assert detail.status_code == 200 and detail.json()['extracted_text'] == '已保存的合成资料'
    finally:
        event.remove(database, 'checkout', constrain)
        event.remove(database, 'handle_error', observe)
        with database.connect() as connection:
            connection.exec_driver_sql(f'PRAGMA max_page_count={previous_limit}')
    recovered = note(http, account, 'retry-note', '受控容量测试内容' * 8000)
    assert recovered.status_code == 201
    replay = note(http, account, 'retry-note', '受控容量测试内容' * 8000)
    assert replay.status_code == 200 and replay.json()['id'] == recovered.json()['id']
    assert counts(database)['sources'] == before['sources'] + 1
    with database.connect() as connection:
        assert connection.exec_driver_sql('PRAGMA integrity_check').scalar_one() == 'ok'


def test_partial_original_write_failure_cleans_staging_and_preserves_original(storage, account, monkeypatch):
    http, database, assets = storage
    original = upload(http, account, 'existing-file').json()
    before = counts(database)
    originals = set(assets.iterdir())
    actual_open = Path.open

    class FullWriter:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.handle.close()

        def write(self, content):
            self.handle.write(content[:3])
            self.handle.flush()
            raise OSError(errno.ENOSPC, 'controlled disk full')

    def failing_open(path, mode='r', *args, **kwargs):
        handle = actual_open(path, mode, *args, **kwargs)
        return FullWriter(handle) if path.parent == assets and mode == 'xb' else handle

    with monkeypatch.context() as scope:
        scope.setattr(Path, 'open', failing_open)
        failed = upload(http, account, 'retry-file')
        assert failed.status_code == 503 and failed.json()['code'] == 'local_storage_full'
        assert '释放空间' in failed.json()['message']
    assert counts(database) == before
    assert set(assets.iterdir()) == originals
    assert http.get(original['file']['download_path'], headers=account['headers']).content == b'name,value\nsynthetic,1\n'
    recovered = upload(http, account, 'retry-file')
    assert recovered.status_code == 201
    assert upload(http, account, 'retry-file').json()['id'] == recovered.json()['id']
    assert len(list(assets.iterdir())) == 2


def test_metadata_commit_failure_removes_new_asset_and_rolls_back_source(storage, account):
    http, database, assets = storage
    original = upload(http, account, 'existing-file').json()
    before = counts(database)
    originals = set(assets.iterdir())
    with database.begin() as connection:
        connection.exec_driver_sql("CREATE TRIGGER reject_asset BEFORE INSERT ON file_assets "
                                   "BEGIN SELECT RAISE(ABORT, 'controlled asset rejection'); END")
    try:
        assert upload(http, account, 'retry-file').status_code == 500
        assert counts(database) == before
        assert set(assets.iterdir()) == originals
        downloaded = http.get(original['file']['download_path'], headers=account['headers'])
        assert downloaded.status_code == 200 and downloaded.content == b'name,value\nsynthetic,1\n'
    finally:
        with database.begin() as connection:
            connection.exec_driver_sql('DROP TRIGGER reject_asset')
    recovered = upload(http, account, 'retry-file')
    assert recovered.status_code == 201
    replay = upload(http, account, 'retry-file')
    assert replay.status_code == 200 and replay.json()['id'] == recovered.json()['id']
    assert counts(database)['sources'] == before['sources'] + 1
    assert len(list(assets.iterdir())) == 2


def test_document_revision_sqlite_full_preserves_supplement_and_original(storage, account):
    http, database, _ = storage
    source = upload(http, account, 'worker-file').json()
    supplemented = http.post(f'/v1/sources/{source["id"]}/supplements', headers=account['headers'],
                             json={'text': '原有用户补充内容'})
    assert supplemented.status_code == 201
    before = counts(database)
    sessions = sessionmaker(bind=database, autoflush=False, expire_on_commit=False)
    with sessions() as session:
        prior_revision = session.get(Source, source['id']).current_revision_id
    with database.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
        connection.exec_driver_sql('VACUUM')
        page_limit = connection.exec_driver_sql('PRAGMA page_count').scalar_one()
        previous_limit = connection.exec_driver_sql('PRAGMA max_page_count').scalar_one()
    observed = []

    def constrain(connection, *_):
        connection.execute(f'PRAGMA max_page_count={page_limit}')

    def observe(context):
        observed.append(str(context.original_exception))

    event.listen(database, 'checkout', constrain)
    event.listen(database, 'handle_error', observe)
    try:
        with sessions() as session:
            assert worker.run_document_once(session, lambda *_: '较大的合成解析结果' * 8000)
        assert any('database or disk is full' in message for message in observed)
        assert counts(database) == before
        with sessions() as session:
            current = session.get(Source, source['id'])
            assert current.current_revision_id == prior_revision and current.processing_status == 'usable'
            job = session.query(DocumentJob).one()
            assert job.status == 'failed' and job.last_error == 'OperationalError'
        detail = http.get(f'/v1/sources/{source["id"]}', headers=account['headers']).json()
        assert detail['extracted_text'] == '原有用户补充内容'
        assert http.get(source['file']['download_path'], headers=account['headers']).content == b'name,value\nsynthetic,1\n'
    finally:
        event.remove(database, 'checkout', constrain)
        event.remove(database, 'handle_error', observe)
        with database.connect() as connection:
            connection.exec_driver_sql(f'PRAGMA max_page_count={previous_limit}')
    # Recover through the production retry API without administrative DB edits.
    retry = http.post(f'/v1/sources/{source["id"]}/document-parse', headers=account['headers'])
    assert retry.status_code == 202
    duplicate = http.post(f'/v1/sources/{source["id"]}/document-parse', headers=account['headers'])
    assert duplicate.status_code == 409
    with sessions() as session:
        assert worker.run_document_once(session, lambda *_: '恢复后的解析结果')
        job = session.query(DocumentJob).one()
        assert job.status == 'completed' and job.attempts == 2 and job.last_error is None
    detail = http.get(f'/v1/sources/{source["id"]}', headers=account['headers']).json()
    assert detail['extracted_text'] == '恢复后的解析结果\n\n原有用户补充内容'
    assert counts(database)['source_revisions'] == before['source_revisions'] + 1


def test_document_start_commit_failure_keeps_pending_job_for_next_worker(storage, account):
    http, database, _ = storage
    source = upload(http, account, 'worker-file').json()
    before = counts(database)
    sessions = sessionmaker(bind=database, autoflush=False, expire_on_commit=False)
    with database.begin() as connection:
        connection.exec_driver_sql('CREATE TRIGGER reject_job_update BEFORE UPDATE ON document_jobs '
                                   "BEGIN SELECT RAISE(ABORT, 'controlled job rejection'); END")
    try:
        with sessions() as session, pytest.raises(IntegrityError):
            worker.run_document_once(session, lambda *_: '不应执行的解析')
        assert counts(database) == before
        with sessions() as session:
            job = session.query(DocumentJob).one()
            assert job.status == 'pending' and job.attempts == 0
            assert session.get(Source, source['id']).processing_status == 'pending'
        assert http.get(source['file']['download_path'], headers=account['headers']).content == b'name,value\nsynthetic,1\n'
    finally:
        with database.begin() as connection:
            connection.exec_driver_sql('DROP TRIGGER reject_job_update')
    # A fresh session represents the next worker attempt after storage recovers.
    with sessions() as session:
        assert worker.run_document_once(session, lambda *_: '恢复后可处理')
        job = session.query(DocumentJob).one()
        assert job.status == 'completed' and job.attempts == 1
    assert counts(database)['source_revisions'] == before['source_revisions'] + 1
