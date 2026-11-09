"""Durable capture orchestration; UI exit does not own a submitted operation."""
import hashlib
from uuid import uuid4

from sqlalchemy import select

from app.capture_store import CaptureStore
from app.config import settings
from app.errors import ApiError
from app.models import IdempotencyRecord, SourceImage, SourceProject
from app.platform_support import get_platform
from app.platform_support.contracts import UnsupportedPlatform
from app.schemas import CaptureCreate, ContextPatch
from app.services import create_capture, patch_context, require_domain, require_source
from app.feishu_capture import archive_images, capture_payload, fetch_document, is_feishu


def existing_source(db, job):
    record = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.owner_id == job['owner_id'],
                       IdempotencyRecord.client_request_id == job['id']))
    if record:
        return require_source(db, job['owner_id'], record.source_id)
    if job.get('source_id'):
        raise ValueError('采集资料回执与任务不一致，请核对原资料')
    return None


def save_file(db, store, job):
    from app.files import finalize_file_capture
    spec = job['input']
    data = store.original(job['id'], job['assets']['file'], settings.max_file_bytes)
    asset_id = str(uuid4())
    path = settings.upload_dir / asset_id
    store.files.atomic_write(path, data)
    retained = False
    try:
        result, replay = finalize_file_capture(db, job['owner_id'], job['id'], spec['domain_id'],
            spec['title'], asset_id, path, len(data), hashlib.sha256(data).hexdigest())
        retained = not replay
        return require_source(db, job['owner_id'], result['id'])
    finally:
        if not retained:
            path.unlink(missing_ok=True)


def save_images(db, store, job, source, document):
    from app.images import finalize_image
    for image in document['images']:
        saved = db.get(SourceImage, image['id'])
        if saved is None or saved.source_id != source.id:
            raise ValueError('图片与资料版本不一致，请核对采集检查点')
        asset = image.get('asset')
        if saved.status == 'saved':
            if asset and saved.sha256 != asset['sha256']:
                raise ValueError('已保存图片与采集检查点内容不同')
            continue
        if not asset:
            if not image['failure_reason']:
                raise ValueError('图片采集检查点缺少原件，请保留任务并核对')
            continue
        store.update(job['id'], message=f"正在保存飞书图片 {image['ordinal'] + 1} / {len(document['images'])}…")
        data = store.original(job['id'], asset, settings.max_image_bytes)
        key = str(uuid4())
        path = settings.upload_dir / 'images' / key
        store.files.atomic_write(path, data)
        retained = False
        try:
            _, replay = finalize_image(db, job['owner_id'], source.id, image['id'], path, key, len(data), asset['sha256'])
            retained = not replay
        finally:
            if not retained:
                path.unlink(missing_ok=True)


def process(db, store, job, external):
    spec = job['input']
    require_domain(db, job['owner_id'], spec['domain_id'])
    source = existing_source(db, job)
    document = store.checkpoint(job['id'])
    if source is None:
        if spec['kind'] == 'file':
            source = save_file(db, store, job)
        else:
            if spec['kind'] == 'link' and is_feishu(spec['text']):
                if document is None:
                    store.update(job['id'], message='正在读取飞书文档…')
                    document = fetch_document(spec['text'], spec['feishu'], external)
                    store.checkpoint(job['id'], document)
                document = archive_images(document, spec['feishu'], external, store, job['id'],
                    lambda message: store.update(job['id'], message=message))
                payload = capture_payload(document, spec)
            else:
                context = {'domain_id': spec['domain_id'], 'project_id': spec['project_id']}
                payload = {'capture_type': 'url' if spec['kind'] == 'link' else 'text', 'title': spec['title'],
                    'content': {'text': '' if spec['kind'] == 'link' else spec['text']},
                    'coverage': 'link_only' if spec['kind'] == 'link' else 'full_text',
                    'context': context, 'client': {'kind': 'desktop', 'version': '0.1.0'}}
                if spec['kind'] == 'link':
                    payload['source_url'] = spec['text'].strip()
            source, _ = create_capture(db, job['owner_id'], job['id'], CaptureCreate.model_validate(payload))
        store.update(job['id'], source_id=source.id, title=source.title)
    if spec['kind'] == 'file' and spec['project_id']:
        projects = set(db.scalars(select(SourceProject.project_id).where(SourceProject.source_id == source.id)))
        if projects and projects != {spec['project_id']}:
            raise ValueError('资料项目已变更，请核对后再继续采集')
        if not projects:
            patch_context(db, job['owner_id'], source, ContextPatch(project_id=spec['project_id']))
    if source.kind == 'feishu_document':
        if document is None:
            raise ValueError('飞书正文已入库，但图片检查点缺失，请核对原资料')
        save_images(db, store, job, source, document)
    store.update(job['id'], state='completed', source_id=source.id, title=source.title, message='资料已归档')


def run_capture_once(db, store=None, external=None):
    if store is None:
        if settings.environment != 'local':
            return False
        store = CaptureStore(settings.local_paths.data)
    try:
        job = store.claim()
    except (ApiError, OSError, ValueError):
        return False  # A device queue failure must not block reading/OCR/indexing.
    if job is None:
        return False
    try:
        process(db, store, job, external or get_platform().external_capabilities())
    except Exception as error:
        db.rollback()
        # Never persist raw CLI stderr, arguments or filesystem exceptions.
        if isinstance(error, ApiError):
            message = error.message
        elif type(error) is ValueError or isinstance(error, UnsupportedPlatform):
            message = str(error)
        else:
            message = '采集归档失败，原件和检查点已保留，请检查磁盘及系统能力后手动重试'
        store.update(job['id'], state='failed', message='发送失败：' + message[:500])
    return True
