"""Atomic handoff and v1 Swift queue migration; drafts are never executed."""
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from sqlalchemy import select

from .capture_contract import CaptureSubmission, ClientAsset, FeishuOptions
from .capture_store import CaptureStore, fingerprint, now
from .config import settings
from .errors import ApiError
from .models import FileAsset, IdempotencyRecord, SourceImage
from .platform_support import get_platform
from .schemas import CaptureCreate
from .services import payload_digest, require_domain, require_project, require_source


def verify_sql_receipt(db, record, source, spec, document):
    """An old SQL key must prove the same frozen content, not just ownership."""
    if spec['kind'] == 'file':
        asset = db.scalar(select(FileAsset).where(FileAsset.source_id == source.id))
        if source.kind != 'file' or asset is None:
            raise ApiError(409, 'capture_receipt_mismatch', '旧采集类型与入库回执不一致')
        checksum = (spec.get('file') or {}).get('sha256') or asset.sha256
        from .files import validate_filename
        expected = hashlib.sha256(json.dumps(['file', spec['domain_id'],
            validate_filename(spec['title']), checksum], ensure_ascii=False).encode()).hexdigest()
    else:
        from .feishu_capture import capture_payload, is_feishu
        if spec['kind'] == 'link' and is_feishu(spec['text']):
            if document is None:
                raise ApiError(409, 'capture_receipt_mismatch', '旧飞书采集缺少冻结正文，原队列已保留')
            body = capture_payload(document, spec)
        else:
            body = {'capture_type': 'url' if spec['kind'] == 'link' else 'text', 'title': spec['title'],
                'content': {'text': '' if spec['kind'] == 'link' else spec['text']},
                'coverage': 'link_only' if spec['kind'] == 'link' else 'full_text',
                'context': {'domain_id': spec['domain_id'], 'project_id': spec['project_id']},
                'client': {'kind': 'desktop', 'version': '0.1.0'}}
            if spec['kind'] == 'link':
                body['source_url'] = spec['text'].strip()
        expected = payload_digest(CaptureCreate.model_validate(body))
    if record.payload_hash != expected:
        raise ApiError(409, 'capture_receipt_mismatch', '此采集编号已保存不同内容，旧队列与原件已保留')


def verify_completed_originals(db, source, store):
    references = []
    if source.kind == 'file':
        asset = db.scalar(select(FileAsset).where(FileAsset.source_id == source.id))
        if asset is None:
            raise ValueError('已完成的旧采集缺少文件回执')
        references.append((settings.upload_dir / asset.id, asset.size_bytes, asset.sha256, settings.max_file_bytes))
    for image in db.scalars(select(SourceImage).where(SourceImage.source_id == source.id, SourceImage.status == 'saved')):
        references.append((settings.upload_dir / 'images' / image.file_key, image.size_bytes, image.sha256, settings.max_image_bytes))
    for path, size, digest, limit in references:
        raw = store.files.read(path, maximum=limit, parents=True)
        if raw is None or len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('已完成的旧采集原件缺失或已变更，旧暂存仍保留')


def receive(db, owner, submission, store):
    require_domain(db, owner, submission.domain_id)
    require_project(db, owner, submission.project_id or None)
    if submission.kind == 'file':
        from .files import validate_filename
        validate_filename(submission.title)
    if submission.kind == 'link':
        from .feishu_capture import checked_link
        try:
            checked_link(submission.text)
        except ValueError as error:
            raise ApiError(422, 'invalid_capture_link', str(error)) from None
    spec = submission.model_dump(mode='json')
    document, original_refs = None, {}
    if submission.legacy_document:
        from .feishu_capture import legacy_document
        document, original_refs = legacy_document(submission.legacy_document, store.client_files)
    digest = fingerprint({'input': spec, 'document': submission.legacy_document})
    with store.locked():
        previous = store.load(submission.id)
        if previous:
            if previous['owner_id'] != owner or previous['fingerprint'] != digest:
                raise ApiError(409, 'capture_handoff_conflict', '此采集编号已经交接不同内容，未覆盖')
            if previous.get('hidden'):
                raise ApiError(409, 'capture_job_removed', '此采集任务已移除，请重新添加资料')
            if previous.get('source_id'):
                require_source(db, owner, previous['source_id'])
            return previous
        known = db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.owner_id == owner, IdempotencyRecord.client_request_id == submission.id))
        source = require_source(db, owner, known.source_id) if known else None
        if known:
            verify_sql_receipt(db, known, source, spec, document)
        if submission.legacy_source_id and (source is None or source.id != submission.legacy_source_id):
            raise ApiError(409, 'capture_receipt_mismatch', '旧采集资料与幂等记录不一致，原队列已保留')
        if submission.legacy_sent and source is None:
            raise ApiError(409, 'capture_receipt_missing', '旧采集缺少可核对的入库回执，原队列已保留')
        if submission.legacy_sent:
            verify_completed_originals(db, source, store)
        value = dict(version=1, id=submission.id, owner_id=owner, fingerprint=digest, input=spec,
            state='completed' if submission.legacy_sent else 'queued', title=submission.title,
            source_id=source.id if source else None, created_at=now(), message='等待后台归档', assets={}, hidden=False)
        try:
            # A prior crash may have left an unpublished private staging tree.
            store.clean(submission.id)
            if not submission.legacy_sent:
                if submission.file and source is None:
                    value['assets']['file'] = store.receive_asset(submission.id, spec['file'], settings.max_file_bytes)
                if submission.legacy_document:
                    saved = {image.id for image in db.scalars(select(SourceImage).where(
                        SourceImage.source_id == source.id, SourceImage.status == 'saved'))} if source else set()
                    for image in document['images']:
                        reference = original_refs.get(image['id'])
                        if reference and image['id'] not in saved:
                            image['asset'] = store.receive_asset(submission.id, reference, settings.max_image_bytes)
                    if sum(image.get('asset', {}).get('size', 0) for image in document['images']) > 200 * 1024 * 1024:
                        raise ValueError('旧飞书图片总量超过限制')
                    store.checkpoint(submission.id, document)
            store.save(value)  # The published record is the durable receipt.
            return value
        except BaseException:
            if store.load(submission.id) is None:
                store.clean(submission.id)
            raise


def legacy_asset(url, root):
    parts = urlsplit(url)
    if parts.scheme != 'file' or parts.netloc not in {'', 'localhost'} or parts.query or parts.fragment:
        raise ValueError('旧采集原件标识无效')
    path = get_platform().client_file_path(Path(unquote(parts.path)))
    # Do not resolve a symlink before checking its ancestry/read safety.
    relative = path.relative_to(root)
    return ClientAsset(key=relative.as_posix()).model_dump()


def migrate_legacy(db, owner, options: FeishuOptions, store: CaptureStore):
    try:
        raw = store.files.read(store.data / 'capture-queue/queue.json', maximum=32 * 1024 * 1024, parents=True)
    except (OSError, ValueError):
        raise ApiError(409, 'legacy_capture_unavailable', '旧采集队列无法安全读取，原文件已保留') from None
    if raw is None:
        return {'items': [], 'errors': []}
    try:
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict) or snapshot.get('version') != 1 or not isinstance(snapshot.get('items'), list):
            raise ValueError()
        identifiers = [item['id'] for item in snapshot['items']]
        if len(set(identifier.lower() for identifier in identifiers)) != len(identifiers):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ApiError(409, 'legacy_capture_invalid', '旧采集队列格式或版本无效，原文件已保留') from None
    receipts, errors = [], []
    for item in snapshot['items']:
        if not item.get('destination'):
            continue  # Unsubmitted selections and drafts remain in the client.
        try:
            if item['destination'] != 'local|' + owner:
                raise ApiError(409, 'legacy_capture_owner_changed', '旧条目属于其他账户或后台，原队列已保留')
            previous = store.load(item['id'])
            if previous and previous['owner_id'] == owner:
                spec = previous['input']
                received = item.get('kernelJobID') and item.get('kernelReceipt') == previous['fingerprint']
                if not received and previous.get('source_id'):
                    require_source(db, owner, previous['source_id'])
                if not received and (spec['kind'] != item['kind'] or spec['text'] != (item.get('text') or '')
                        or spec['domain_id'] != (item.get('domainID') or snapshot.get('domainID') or '')
                        or (spec['project_id'] or None) != (item.get('projectID') or None)):
                    raise ApiError(409, 'capture_handoff_conflict', '旧条目与已接收内容不同，原件已保留')
                stored_checksum = (previous['assets'].get('file') or spec.get('file') or {}).get('sha256')
                if not received and item.get('fileDigest') and stored_checksum and item['fileDigest'] != stored_checksum:
                    raise ApiError(409, 'capture_handoff_conflict', '旧条目文件摘要已变更，原件已保留')
                if not previous.get('hidden'):
                    receipts.append(previous)
                continue
            file = legacy_asset(item['file'], store.client_files) if item.get('file') else None
            if file and item.get('fileDigest'):
                file['sha256'] = item['fileDigest']
            submission = CaptureSubmission(id=item['id'], kind=item['kind'], title=item['title'],
                text=item.get('text') or '', domain_id=item.get('domainID') or snapshot.get('domainID') or '',
                project_id=item.get('projectID') or None, file=file, feishu=options,
                legacy_source_id=item.get('sourceID'), legacy_document=item.get('feishuDocument'),
                legacy_sent=bool(item.get('sent')))
            receipts.append(receive(db, owner, submission, store))
        except Exception as error:
            errors.append({'id': item.get('id'), 'message': error.message if isinstance(error, ApiError)
                           else '旧采集条目交接失败，原文件已保留，请检查暂存与资料回执'})
    return {'items': receipts, 'errors': errors}
