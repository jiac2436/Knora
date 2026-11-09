"""Raw file ingestion. Files are stored without parsing or executable serving."""
import hashlib
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import FileResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import AppPrincipal, require_app_principal
from .config import settings
from .database import get_db
from .errors import ApiError
from .models import DocumentJob, FileAsset, IdempotencyRecord, OCRJob, OCRPreference, PersonalContext, Source
from .platform_support import get_platform
from .services import require_domain, require_source, source_view

router = APIRouter()
DOCUMENT_EXTENSIONS = frozenset({
    ".doc", ".docx", ".docm", ".ppt", ".pps", ".pot", ".pptx", ".pptm", ".ppsx", ".ppsm",
    ".xls", ".xlsx", ".xlsm", ".xlsb", ".odt", ".ods", ".odp", ".rtf", ".epub", ".csv", ".md", ".txt",
})


def validate_filename(filename):
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if not filename.strip() or any(ord(c) < 32 for c in filename):
        raise ApiError(422, "invalid_filename", "文件名无效")
    extension = Path(filename).suffix.lower()
    if extension not in DOCUMENT_EXTENSIONS and extension != ".pdf":
        raise ApiError(422, "unsupported_file_type", "仅支持 PDF、Word、PowerPoint、Excel、OpenDocument、RTF、EPUB、CSV、TXT 和 Markdown")
    return filename


def finalize_file_capture(db, owner_id, idempotency_key, domain_id, filename, asset_id, path, size, sha256):
    """Shared transaction for HTTP uploads and durable capture jobs."""
    require_domain(db, owner_id, domain_id)
    filename = validate_filename(filename)
    digest = hashlib.sha256(json.dumps(
        ["file", domain_id, filename, sha256], ensure_ascii=False
    ).encode()).hexdigest()

    def replay():
        record = db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.owner_id == owner_id,
            IdempotencyRecord.client_request_id == idempotency_key,
        ))
        if record is None:
            return None
        if record.payload_hash != digest:
            raise ApiError(409, "idempotency_conflict", "相同幂等键已用于不同请求")
        source = db.get(Source, record.source_id)
        if source is None or source.deleted_at is not None:
            raise ApiError(409, "idempotency_result_deleted", "原上传结果已删除")
        return source_view(db, source, detail=True), True

    previous = replay()
    if previous is not None:
        return previous
    with path.open("rb") as uploaded:
        is_pdf = uploaded.read(5) == b"%PDF-"
    if Path(filename).suffix.lower() == ".pdf" and not is_pdf:
        raise ApiError(422, "invalid_pdf", "文件内容不是 PDF")
    source = Source(owner_id=owner_id, domain_id=domain_id, kind="file",
                    title=filename, coverage="file_only", processing_status="pending",
                    client_kind="desktop", client_version="0.1.0")
    db.add(source)
    db.flush()
    db.add(FileAsset(id=asset_id, source_id=source.id, filename=filename,
                     size_bytes=size, sha256=sha256))
    db.add(PersonalContext(source_id=source.id))
    if is_pdf:
        preference = db.get(OCRPreference, owner_id)
        db.add(OCRJob(owner_id=owner_id, source_id=source.id,
                      model=preference.model if preference else "small",
                      engine=preference.engine if preference else "pdfium"))
    else:
        db.add(DocumentJob(owner_id=owner_id, source_id=source.id))
    db.add(IdempotencyRecord(owner_id=owner_id, client_request_id=idempotency_key,
                             payload_hash=digest, source_id=source.id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        previous = replay()
        if previous is not None:
            return previous
        raise
    return source_view(db, source, detail=True), False


@router.post("/v1/file-captures", status_code=201)
async def upload_file(
    request: Request,
    response: Response,
    domain_id: str = Query(min_length=1),
    filename: str = Query(min_length=1, max_length=500),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
):
    require_domain(db, principal.owner_id, domain_id)
    filename = validate_filename(filename)
    settings.upload_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    asset_id = str(uuid.uuid4())
    path = settings.upload_dir / asset_id
    committed = False
    try:
        size = 0
        checksum = hashlib.sha256()
        with path.open("xb") as output:
            get_platform().files.protect(path)
            async for chunk in request.stream():
                size += len(chunk)
                if size > settings.max_file_bytes:
                    raise ApiError(413, "file_too_large", "文件超过服务器大小限制")
                checksum.update(chunk)
                output.write(chunk)
        result, replayed = finalize_file_capture(db, principal.owner_id, idempotency_key, domain_id,
            filename, asset_id, path, size, checksum.hexdigest())
        committed = not replayed
        if replayed:
            response.status_code = 200
            response.headers["Idempotent-Replay"] = "true"
        return result
    finally:
        if not committed:
            path.unlink(missing_ok=True)


@router.get("/v1/sources/{source_id}/file")
def download_file(source_id: str, principal: AppPrincipal = Depends(require_app_principal),
                  db: Session = Depends(get_db)):
    require_source(db, principal.owner_id, source_id)
    asset = db.scalar(select(FileAsset).where(FileAsset.source_id == source_id))
    if asset is None or not (settings.upload_dir / asset.id).is_file():
        raise ApiError(404, "not_found", "原文件不存在")
    return FileResponse(settings.upload_dir / asset.id, filename=asset.filename,
                        media_type="application/octet-stream",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


@router.post('/v1/sources/{source_id}/document-parse', status_code=202)
def retry_document(source_id: str, principal: AppPrincipal = Depends(require_app_principal),
                   db: Session = Depends(get_db)) -> dict:
    source = require_source(db, principal.owner_id, source_id)
    asset = db.scalar(select(FileAsset).where(FileAsset.source_id == source.id))
    if asset is None or Path(asset.filename).suffix.lower() not in DOCUMENT_EXTENSIONS:
        raise ApiError(422, 'not_document', '该资料不是可解析的普通文档')
    path = settings.upload_dir / asset.id
    if not path.is_file():
        raise ApiError(404, 'original_missing', '原文件不存在，无法重新解析')
    with path.open('rb') as original:
        if original.read(5) == b'%PDF-':
            raise ApiError(422, 'not_document', 'PDF 请使用重新扫描')
    active = db.scalar(select(DocumentJob.id).where(DocumentJob.source_id == source.id,
                                                   DocumentJob.status.in_(['pending', 'running'])))
    if active:
        raise ApiError(409, 'document_parse_in_progress', '该文档已有解析任务在进行')
    job = db.scalar(select(DocumentJob).where(DocumentJob.source_id == source.id)
                    .order_by(DocumentJob.created_at.desc(), DocumentJob.id.desc()).limit(1))
    from .runtime_extensions import MANUAL_EDIT_PRESERVED
    if job is None or not (job.status == 'failed' or
            (job.status == 'cancelled' and job.last_error == MANUAL_EDIT_PRESERVED)):
        raise ApiError(409, 'document_parse_not_failed', '只有解析失败的文档可以重试')
    changed = db.execute(update(DocumentJob).where(DocumentJob.id == job.id, DocumentJob.status == job.status)
                         .values(status='pending', last_error=None, completed_at=None))
    if changed.rowcount != 1:
        db.rollback()
        raise ApiError(409, 'document_parse_in_progress', '该文档已有解析任务在进行')
    source.processing_status = 'pending'
    db.commit()
    return {'id': job.id, 'source_id': source.id, 'status': 'pending'}
