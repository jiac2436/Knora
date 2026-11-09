"""Local Owner capture queue API; no read-only Agent can submit or retry work."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import AppPrincipal, require_app_principal
from .capture_contract import CaptureSubmission, FeishuOptions
from .capture_handoff import migrate_legacy, receive
from .capture_store import CaptureStore
from .config import settings
from .database import get_db
from .errors import ApiError
from .local_trash import require_local
from .models import DocumentJob, OCRJob, Source, SourceImage, utcnow

router = APIRouter(prefix='/v1/capture-queue', dependencies=[Depends(require_local)])


def store():
    return CaptureStore(settings.local_paths.data)


def view(db, job):
    spec = job['input']
    result = {key: job[key] for key in ('version', 'id', 'fingerprint', 'state', 'title', 'source_id',
                                       'created_at', 'updated_at', 'message')}
    result.update(kind=spec['kind'], domain_id=spec['domain_id'], project_id=spec['project_id'],
                  accepted=True, hidden=bool(job.get('hidden')))
    if job['state'] != 'completed':
        return result
    source = db.get(Source, job['source_id']) if job['source_id'] else None
    if source is None or source.owner_id != job['owner_id'] or source.deleted_at is not None:
        result['message'] = '原资料已删除 · 不会重复采集'
        return result
    result['title'] = source.title
    if source.kind == 'file':
        document = db.scalar(select(DocumentJob).where(DocumentJob.source_id == source.id)
                             .order_by(DocumentJob.created_at.desc()).limit(1))
        ocr = db.scalar(select(OCRJob).where(OCRJob.source_id == source.id).order_by(OCRJob.created_at.desc()).limit(1))
        task = document or ocr
        from .runtime_extensions import job_status
        status = job_status(task) if task else ''
        result['message'] = {'pending': '文档已上传 · 等待后台解析', 'running': '文档解析中…',
            'completed': '文档已解析 · 可搜索', 'waiting_extension': '原文件已保存 · 等待安装扩展', 'failed': '文档解析失败 · 请在阅读器查看原因'}.get(status, '原文件已保存 · 未解析')
    elif source.kind == 'feishu_document':
        from .services import current_web_base_revision
        base = current_web_base_revision(db, source)
        images = list(db.scalars(select(SourceImage).where(SourceImage.source_id == source.id,
                                 SourceImage.revision_id == (base.id if base else source.current_revision_id))))
        saved = sum(image.status == 'saved' for image in images)
        result['message'] = f'飞书正文已保存 · 图片 {saved} / {len(images)}' + (' · 部分图片未获取' if saved < len(images) else '')
    elif source.kind == 'url':
        result['message'] = {'usable': '网页正文已读取 · 可搜索', 'failed': '网页读取失败 · 原始链接已保留',
            'processing': '正在读取网页…', 'pending': '链接已保存 · 等待后台读取网页'}.get(source.processing_status, '链接已保存')
    else:
        result['message'] = '笔记已保存'
    return result


@router.post('', status_code=202)
def submit(payload: CaptureSubmission, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    if payload.legacy_source_id or payload.legacy_document or payload.legacy_sent:
        raise ApiError(422, 'legacy_capture_only', '旧队列字段只能由迁移入口读取')
    return view(db, receive(db, principal.owner_id, payload, store()))


@router.post('/migrate')
def migrate(payload: FeishuOptions, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    result = migrate_legacy(db, principal.owner_id, payload, store())
    result['items'] = [view(db, item) for item in result['items']]
    return result


@router.get('')
def list_jobs(offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
              principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    journal = store()
    values = [job for job in journal.entries(principal.owner_id) if not job.get('hidden')]
    return {'items': [view(db, item) for item in values[offset:offset + limit]],
            'warnings': getattr(journal, 'warnings', []),
            'next_offset': offset + limit if offset + limit < len(values) else None}


@router.get('/{identifier}')
def get_job(identifier: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    return view(db, store().load(identifier, principal.owner_id))


@router.post('/{identifier}/retry', status_code=202)
def retry(identifier: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    journal = store()
    with journal.locked():
        job = journal.load(identifier, principal.owner_id)
        if job.get('hidden') or job['state'] != 'failed':
            raise ApiError(409, 'capture_not_retryable', '任务未失败或已移除，请刷新状态')
        job.update(state='queued', message='等待后台继续归档')
        journal.save(job)
    return view(db, job)


@router.delete('/{identifier}')
def dismiss(identifier: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    journal = store()
    with journal.locked():
        job = journal.load(identifier, principal.owner_id)
        if job['state'] in {'queued', 'running'}:
            raise ApiError(409, 'capture_in_progress', '后台正在归档此条目，请等待完成后再移除')
        if job['state'] == 'failed' and job.get('source_id'):
            source = db.get(Source, job['source_id'])
            if source and source.owner_id == principal.owner_id and source.deleted_at is None:
                pending = list(db.scalars(select(SourceImage).where(SourceImage.source_id == source.id,
                                         SourceImage.status == 'pending')))
                for image in pending:
                    image.status = 'unavailable'
                    image.failure_reason = '采集任务已移除，图片原件未完成归档'
                if pending:
                    source.coverage = 'partial_text'
                    source.updated_at = utcnow()
                    db.commit()
        job['hidden'] = True
        journal.save(job)  # Keep the receipt so an old client cannot resubmit it.
        journal.clean(identifier)
    return {'id': job['id'], 'removed': True}
