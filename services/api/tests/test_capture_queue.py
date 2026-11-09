"""Synthetic handoff, restart and migration acceptance; no real CLI/model calls."""
from dataclasses import replace
import hashlib
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from PIL import Image
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import capture_handoff, capture_queue, files, images, local_trash
from app.capture_store import CaptureStore
from app.capture_contract import CaptureSubmission
from app.database import engine
from app.models import IdempotencyRecord, Source, SourceImage, SourceProject
from services.worker import capture as worker


@pytest.fixture
def queue(client, account, tmp_path, monkeypatch):
    journal = CaptureStore(tmp_path.resolve())
    monkeypatch.setattr(capture_queue, 'store', lambda: journal)
    monkeypatch.setattr(local_trash, 'settings', SimpleNamespace(environment='local'))
    for module in (files, images, worker, capture_handoff):
        monkeypatch.setattr(module, 'settings', replace(module.settings, upload_dir=tmp_path / 'assets'))
    return journal


def submission(account, **changes):
    return {'id': str(uuid4()).upper(), 'kind': 'note', 'title': '合成采集', 'text': '合成正文',
            'domain_id': account['domain']['id'], **changes}


def step(journal, external=None):
    with Session(engine) as db:
        return worker.run_capture_once(db, journal, external or FakeFeishu())


def submit(client, account, value):
    result = client.post('/v1/capture-queue', headers=account['headers'], json=value)
    assert result.status_code == 202, result.text
    return result.json()


def stage(journal, data):
    key = str(uuid4())
    journal.files.atomic_write(journal.client_files / key, data)
    return {'key': key, 'sha256': hashlib.sha256(data).hexdigest()}


def test_receipt_duplicate_restart_and_cleanup(client, account, queue):
    value = submission(account)
    receipt = submit(client, account, value)
    assert receipt['accepted'] and receipt['state'] == 'queued'
    assert submit(client, account, value)['fingerprint'] == receipt['fingerprint']
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Source)) == 0
    # Crash after SQL commit, before the execution checkpoint/response is saved.
    original_update = queue.update
    def interrupted(identifier, **changes):
        if changes.get('state') == 'completed':
            raise KeyboardInterrupt()
        return original_update(identifier, **changes)
    queue.update = interrupted
    with pytest.raises(KeyboardInterrupt):
        step(queue)
    assert queue.load(value['id'])['state'] == 'running'
    queue.update = original_update
    queue.recover()
    assert step(queue)
    result = client.get('/v1/capture-queue/' + value['id'], headers=account['headers']).json()
    assert result['state'] == 'completed' and result['message'] == '笔记已保存'
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Source)) == 1
        assert db.scalar(select(IdempotencyRecord)).client_request_id == value['id']
    assert client.delete('/v1/capture-queue/' + value['id'], headers=account['headers']).status_code == 200
    assert queue.load(value['id'])['hidden'] and not queue.work(value['id']).exists()
    assert client.post('/v1/capture-queue', headers=account['headers'], json=value).status_code == 409


def test_file_handoff_original_is_owned_and_project_survives(client, account, queue):
    original = b'column,value\nsynthetic,1\n'
    asset = stage(queue, original)
    value = submission(account, kind='file', title='synthetic.csv', text='', file=asset,
                       project_id=account['project']['id'])
    submit(client, account, value)
    (queue.client_files / asset['key']).unlink()  # Client can now release its copy.
    assert submit(client, account, value)['state'] == 'queued'
    assert step(queue)
    result = client.get('/v1/capture-queue/' + value['id'], headers=account['headers']).json()
    assert result['state'] == 'completed'
    with Session(engine) as db:
        assert db.scalar(select(SourceProject)).project_id == account['project']['id']
        assert db.scalar(select(func.count()).select_from(Source)) == 1
    path = client.get('/v1/sources/' + result['source_id'] + '/file', headers=account['headers'])
    assert path.content == original


@pytest.mark.parametrize('kind', ['hash', 'parent-link', 'file-link', 'outside', 'missing'])
def test_unsafe_handoff_preserves_client_original(client, account, queue, tmp_path, kind):
    asset = stage(queue, b'preserved')
    original = queue.client_files / asset['key']
    if kind == 'hash':
        asset['sha256'] = '0' * 64
    elif kind == 'file-link':
        target = tmp_path / 'original'
        target.write_bytes(b'preserved')
        original.unlink()
        original.symlink_to(target)
    elif kind == 'parent-link':
        link = queue.client_files / 'link'
        link.symlink_to(queue.client_files, target_is_directory=True)
        asset['key'] = 'link/' + asset['key']
    elif kind == 'outside':
        asset['key'] = '../outside'
    else:
        asset['key'] = str(uuid4())
    value = submission(account, kind='file', title='synthetic.csv', text='', file=asset)
    response = client.post('/v1/capture-queue', headers=account['headers'], json=value)
    assert response.status_code in {409, 422}, response.text
    assert original.read_bytes() == b'preserved'
    assert not queue.path(value['id']).exists()


def test_conflict_failure_retry_and_no_automatic_replay(client, account, queue, monkeypatch):
    value = submission(account)
    submit(client, account, value)
    assert client.post('/v1/capture-queue', headers=account['headers'], json={**value, 'text': 'changed'}).status_code == 409
    def failed(*_):
        raise OSError('synthetic-sensitive-path')
    monkeypatch.setattr(worker, 'process', failed)
    assert step(queue)
    assert queue.load(value['id'])['state'] == 'failed'
    assert 'sensitive' not in queue.load(value['id'])['message']
    queue.recover()
    assert not step(queue)
    response = client.post('/v1/capture-queue/' + value['id'] + '/retry', headers=account['headers'])
    assert response.status_code == 202
    assert client.delete('/v1/capture-queue/' + value['id'], headers=account['headers']).status_code == 409


def snapshot(queue, items, domain):
    value = {'version': 1, 'draft': '保留草稿', 'items': items, 'domainID': domain, 'projectID': ''}
    queue.files.atomic_write(queue.data / 'capture-queue/queue.json', json.dumps(value).encode())
    return value


def test_legacy_migration_drafts_and_interruption(client, account, queue, monkeypatch):
    value = submission(account)
    legacy = {'id': value['id'], 'kind': 'note', 'title': value['title'], 'text': value['text'],
        'destination': 'local|' + account['user']['id'], 'domainID': value['domain_id'], 'sent': False}
    pending = {**legacy, 'id': str(uuid4()), 'destination': None}
    before = snapshot(queue, [legacy, pending], value['domain_id'])
    migrate = lambda: client.post('/v1/capture-queue/migrate', headers=account['headers'], json={})
    first = migrate()
    assert first.status_code == 200, first.text
    assert [r['id'] for r in first.json()['items']] == [legacy['id']]
    assert migrate().json()['items'][0]['fingerprint'] == first.json()['items'][0]['fingerprint']
    assert json.loads((queue.data / 'capture-queue/queue.json').read_bytes()) == before
    assert step(queue)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Source)) == 1
    # New client stores only the durable reference, without resurrecting old payloads.
    receipt = first.json()['items'][0]
    updated = dict(legacy, text=None, kernelJobID=legacy['id'], kernelReceipt=receipt['fingerprint'])
    snapshot(queue, [updated, pending], value['domain_id'])
    assert not migrate().json()['errors']


def test_legacy_migration_missing_original_and_bad_version_are_preserved(client, account, queue):
    value = submission(account)
    legacy = {'id': value['id'], 'kind': 'file', 'title': 'legacy.csv', 'file': (queue.client_files / str(uuid4())).as_uri(),
        'destination': 'local|' + account['user']['id'], 'domainID': value['domain_id'], 'sent': False}
    before = snapshot(queue, [legacy], value['domain_id'])
    result = client.post('/v1/capture-queue/migrate', headers=account['headers'], json={})
    assert result.status_code == 200 and result.json()['errors'] and not result.json()['items']
    assert json.loads((queue.data / 'capture-queue/queue.json').read_bytes()) == before
    before['version'] = 99
    queue.files.atomic_write(queue.data / 'capture-queue/queue.json', json.dumps(before).encode())
    assert client.post('/v1/capture-queue/migrate', headers=account['headers'], json={}).status_code == 409


class FakeFeishu:
    def __init__(self, revision=7, preview=False):
        self.revision = revision
        self.preview = preview
        self.calls = []
        output = BytesIO()
        Image.new('RGB', (2, 2), 'white').save(output, 'PNG')
        self.image = output.getvalue()

    def resolve_cli(self, path=''):
        return '/synthetic/lark-cli'

    def run_cli(self, executable, arguments, timeout, directory, maximum_directory_bytes):
        self.calls.append(arguments)
        if '--version' in arguments:
            return SimpleNamespace(output=b'1.0.97', errors=b'', exit_code=0)
        if '+fetch' in arguments:
            if 'xml' in arguments:
                content = '<img src="ImageToken" id="Block1" alt="重要图表"/>'
                revision = self.revision
            else:
                content, revision = '# 合成飞书\n正文\n![](ImageToken)', 7
            value = {'ok': True, 'identity': 'user', 'data': {'document': {'document_id': 'Document',
                     'revision_id': revision, 'content': content}}}
        elif '+media-download' in arguments and self.preview:
            return SimpleNamespace(output=b'', errors=json.dumps({'error': {'type': 'authorization',
                'subtype': 'permission_denied', 'message': 'export permission denied'}}).encode(), exit_code=1)
        else:
            file = directory / 'image.png'
            file.write_bytes(self.image)
            value = {'ok': True, 'data': {'saved_path': str(file)}}
        return SimpleNamespace(output=json.dumps(value).encode(), errors=b'', exit_code=0)

    def recognize_image(self, path):
        return '图片中的合成指标137'


def test_feishu_snapshot_preview_original_and_restart_after_partial_save(client, account, queue, monkeypatch):
    value = submission(account, kind='link', text='https://synthetic.feishu.cn/docx/Document#selection')
    submit(client, account, value)
    external = FakeFeishu(preview=True)
    original = worker.save_images
    def interrupted(*_):
        raise KeyboardInterrupt()
    monkeypatch.setattr(worker, 'save_images', interrupted)
    with pytest.raises(KeyboardInterrupt):
        step(queue, external)
    checkpoint = queue.checkpoint(value['id'])
    image_id = checkpoint['images'][0]['id']
    assert checkpoint['images'][0]['retrieval_method'] == 'preview'
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Source)) == 1
    queue.recover()
    monkeypatch.setattr(worker, 'save_images', original)
    external.calls.clear()
    assert step(queue, external)
    assert not external.calls, '已冻结正文和图片不应重复调用CLI'
    with Session(engine) as db:
        source = db.scalar(select(Source))
        image = db.get(SourceImage, image_id)
        assert image.status == 'saved' and image.retrieval_method == 'preview'
        assert image.ocr_text == '图片中的合成指标137'
        assert db.scalar(select(func.count()).select_from(Source)) == 1
    downloaded = client.get(f'/v1/sources/{source.id}/images/{image_id}', headers=account['headers'])
    assert downloaded.content == external.image


def test_feishu_version_mismatch_creates_no_source(client, account, queue):
    value = submission(account, kind='link', text='https://synthetic.feishu.cn/wiki/Document')
    submit(client, account, value)
    assert step(queue, FakeFeishu(revision=8))
    assert queue.load(value['id'])['state'] == 'failed'
    assert '版本不一致' in queue.load(value['id'])['message']
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Source)) == 0


def test_optional_ocr_failure_preserves_valid_image(client, account, queue):
    value = submission(account, kind='link', text='https://synthetic.feishu.cn/docx/Document')
    submit(client, account, value)
    external = FakeFeishu()
    def unavailable(_):
        raise ValueError('synthetic OCR timeout')
    external.recognize_image = unavailable
    assert step(queue, external)
    assert queue.load(value['id'])['state'] == 'completed'
    with Session(engine) as db:
        image = db.scalar(select(SourceImage))
        assert image.status == 'saved' and image.ocr_text == ''


def test_owner_and_read_only_agent_cannot_access_other_queue(client, account, queue):
    value = submission(account)
    submit(client, account, value)
    second = client.post('/v1/dev/sessions', json={'email': 'other-queue@example.com', 'name': 'Other'}).json()
    other = {'Authorization': 'Bearer ' + second['access_token']}
    assert client.get('/v1/capture-queue/' + value['id'], headers=other).status_code == 404
    assert not client.get('/v1/capture-queue', headers=other).json()['items']
    token = client.post('/v1/agent-connections', headers=account['headers'], json={'client_name': 'synthetic',
        'domain_ids': [account['domain']['id']]}).json()['access_token']
    for method, path in [('GET', '/v1/capture-queue'), ('POST', '/v1/capture-queue/' + value['id'] + '/retry')]:
        assert client.request(method, path, headers={'Authorization': 'Bearer ' + token}).status_code in {401, 403}


@pytest.mark.parametrize('published', [False, True])
def test_handoff_write_failure_never_removes_client_original(client, account, queue, published):
    asset = stage(queue, b'synthetic,137\n')
    value = submission(account, kind='file', title='sample.csv', text='', file=asset)
    original = queue.save
    def fail(record):
        if published:
            original(record)
        raise OSError('synthetic disk full')
    queue.save = fail
    with Session(engine) as db, pytest.raises(OSError):
        capture_handoff.receive(db, account['user']['id'], CaptureSubmission.model_validate(value), queue)
    assert (queue.client_files / asset['key']).read_bytes() == b'synthetic,137\n'
    assert queue.path(value['id']).exists() == published
    assert queue.work(value['id']).exists() == published
    queue.save = original
    submit(client, account, value)
    assert step(queue)


def test_corrupted_execution_record_is_retained_without_blocking_other_jobs(client, account, queue):
    broken_id = str(uuid4())
    queue.files.atomic_write(queue.path(broken_id), b'{corrupted synthetic journal')
    value = submission(account)
    submit(client, account, value)
    queue.recover()
    assert step(queue)
    page = client.get('/v1/capture-queue', headers=account['headers']).json()
    assert page['warnings'] and page['items'][0]['state'] == 'completed'
    assert queue.path(broken_id).read_bytes() == b'{corrupted synthetic journal'
    assert client.get('/v1/capture-queue/' + broken_id, headers=account['headers']).status_code == 409


def test_legacy_body_and_partially_uploaded_images_resume_without_cli(client, account, queue):
    external = FakeFeishu()
    identifier = str(uuid4()).upper()
    image_ids = [str(uuid4()), str(uuid4())]
    payload = {'capture_type': 'feishu_document', 'title': '旧飞书正文',
        'source_url': 'https://synthetic.feishu.cn/docx/Document', 'coverage': 'full_text',
        'content': {'text': '# 旧正文\n![图1](know-image://' + image_ids[0] + ')'},
        'context': {'domain_id': account['domain']['id']}, 'client': {'kind': 'desktop', 'version': '0.1.0'},
        'feishu_document': {'document_id': 'Document', 'revision_id': 7, 'cli_version': '1.0.97'},
        'feishu_images': [{'id': image_id, 'ordinal': i, 'block_id': f'Block{i}', 'caption': f'图{i}'}
                          for i, image_id in enumerate(image_ids)]}
    response = client.post('/v1/captures', headers={**account['headers'], 'Idempotency-Key': identifier}, json=payload)
    assert response.status_code == 201, response.text
    source_id = response.json()['id']
    uploaded = client.put(f'/v1/sources/{source_id}/images/{image_ids[0]}', headers=account['headers'], content=external.image)
    assert uploaded.status_code == 201
    pending = stage(queue, external.image)
    legacy_images = [{'id': image_id, 'ordinal': i, 'token': 'ImageToken', 'blockID': f'Block{i}',
        'caption': f'图{i}', 'failureReason': '', 'ocrText': '', 'retrievalMethod': 'download',
        'file': (queue.client_files / (pending['key'] if i else str(uuid4()))).as_uri()}
        for i, image_id in enumerate(image_ids)]
    legacy = {'id': identifier, 'kind': 'link', 'title': '旧飞书正文', 'text': payload['source_url'],
        'destination': 'local|' + account['user']['id'], 'domainID': account['domain']['id'], 'sourceID': source_id,
        'sent': False, 'uploadedImageIDs': [image_ids[0]], 'feishuDocument': {'markdown': payload['content']['text'],
            'title': '旧飞书正文', 'documentID': 'Document', 'revisionID': 7, 'cliVersion': '1.0.97',
            'partial': False, 'images': legacy_images}}
    before = snapshot(queue, [legacy], account['domain']['id'])
    result = client.post('/v1/capture-queue/migrate', headers=account['headers'], json={})
    assert result.status_code == 200 and not result.json()['errors'], result.text
    assert step(queue, external)
    assert not external.calls, '已有正文和图片清单不得重新抓取'
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Source)) == 1
        assert all(db.get(SourceImage, image_id).status == 'saved' for image_id in image_ids)
    assert json.loads((queue.data / 'capture-queue/queue.json').read_bytes()) == before


def test_completed_legacy_original_is_verified_before_receipt(client, account, queue):
    identifier = str(uuid4())
    raw = b'legacy,137\n'
    response = client.post('/v1/file-captures', headers={**account['headers'], 'Idempotency-Key': identifier},
        params={'domain_id': account['domain']['id'], 'filename': 'old.csv'}, content=raw)
    assert response.status_code == 201
    staged = stage(queue, raw)
    legacy = {'id': identifier, 'kind': 'file', 'title': 'old.csv', 'file': (queue.client_files / staged['key']).as_uri(),
        'destination': 'local|' + account['user']['id'], 'domainID': account['domain']['id'],
        'sourceID': response.json()['id'], 'sent': True}
    snapshot(queue, [legacy], account['domain']['id'])
    # Corruption of the already archived file must not cause the only good client copy to be released.
    for path in files.settings.upload_dir.iterdir():
        if path.is_file():
            path.unlink()
    result = client.post('/v1/capture-queue/migrate', headers=account['headers'], json={})
    assert result.json()['errors'] and not result.json()['items']
    assert (queue.client_files / staged['key']).read_bytes() == raw


@pytest.mark.parametrize('url', ['file:///private/file', 'http://user:secret@example.com/',
    'https://synthetic.feishu.cn/base/Unsupported'])
def test_invalid_link_does_not_create_an_executable_job(client, account, queue, url):
    value = submission(account, kind='link', text=url)
    response = client.post('/v1/capture-queue', headers=account['headers'], json=value)
    assert response.status_code == 422
    assert not queue.path(value['id']).exists()


def test_permanent_source_cleanup_erases_device_copy_but_keeps_deduplication(client, account, queue):
    asset = stage(queue, b'private synthetic original')
    value = submission(account, kind='file', title='original.csv', text='', file=asset)
    receipt = submit(client, account, value)
    assert step(queue)
    record = queue.load(value['id'])
    assert queue.work(value['id']).exists()
    queue.purge_source(account['user']['id'], record['source_id'], {value['id']})
    after = queue.load(value['id'])
    assert after['state'] == 'completed' and after['fingerprint'] == receipt['fingerprint']
    assert after['input']['text'] == '' and not after['assets']
    assert not queue.work(value['id']).exists()
    assert not step(queue)


def test_existing_sql_receipt_cannot_acknowledge_changed_handoff(client, account, queue):
    value = submission(account)
    payload = {'capture_type': 'text', 'title': value['title'], 'content': {'text': value['text']},
        'coverage': 'full_text', 'context': {'domain_id': value['domain_id']},
        'client': {'kind': 'desktop', 'version': '0.1.0'}}
    result = client.post('/v1/captures', headers={**account['headers'], 'Idempotency-Key': value['id']}, json=payload)
    assert result.status_code == 201
    response = client.post('/v1/capture-queue', headers=account['headers'], json={**value, 'text': '不同的未保存正文'})
    assert response.status_code == 409 and response.json()['code'] == 'capture_receipt_mismatch'
    assert not queue.path(value['id']).exists()
    assert submit(client, account, value)['accepted']
    assert step(queue)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(Source)) == 1


def test_dismissing_failed_partial_archive_settles_pending_media(client, account, queue, monkeypatch):
    value = submission(account, kind='link', text='https://synthetic.feishu.cn/docx/Document')
    submit(client, account, value)
    def interrupted(*_):
        raise OSError('synthetic upload failure')
    monkeypatch.setattr(worker, 'save_images', interrupted)
    assert step(queue, FakeFeishu())
    record = queue.load(value['id'])
    assert record['state'] == 'failed' and record['source_id']
    assert client.delete('/v1/capture-queue/' + value['id'], headers=account['headers']).status_code == 200
    with Session(engine) as db:
        image = db.scalar(select(SourceImage))
        source = db.get(Source, record['source_id'])
        assert image.status == 'unavailable' and '任务已移除' in image.failure_reason
        assert source.coverage == 'partial_text'
    assert not queue.work(value['id']).exists()
