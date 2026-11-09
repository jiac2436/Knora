"""Local-only trash; permanent deletion remains owned by the worker."""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Query
from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from .auth import AppPrincipal, require_app_principal
from .config import settings
from .database import get_db
from .errors import ApiError
from .models import DomainTrash, DeletionJob, DocumentJob, FileAsset, OCRJob, Source, SourceImage, WebReadJob, utcnow
from .services import content_type, reindex_source

CLEANUP_STATUSES = ("pending", "retry", "running", "failed", "completed")


def require_local() -> None:
    if settings.environment != "local":
        raise ApiError(404, "not_found", "接口不存在")


router = APIRouter(prefix="/v1/trash", dependencies=[Depends(require_local)])


def trash_source(db: Session, owner_id: str, source_id: str) -> Source:
    source = db.scalar(select(Source).where(
        Source.id == source_id, Source.owner_id == owner_id,
        Source.deleted_at.is_not(None), Source.processing_status != "deleted"))
    if source is None:
        raise ApiError(404, "not_found", "回收站资料不存在")
    return source


def verify_asset(relative: str, size: int | None, digest: str | None) -> None:
    if any(path.is_symlink() for path in (settings.upload_dir, *settings.upload_dir.parents)):
        raise ApiError(409, "trash_asset_unavailable", "资料原件目录不可用，无法恢复")
    root = settings.upload_dir.resolve()
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts) or "\\" in relative or "\x00" in relative:
        raise ApiError(409, "trash_asset_unavailable", "资料原件路径无效，无法恢复")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ApiError(409, "trash_asset_unavailable", "资料原件不可用，无法恢复")
    if not path.is_file() or size is None or digest is None or path.stat().st_size != size:
        raise ApiError(409, "trash_asset_unavailable", "资料原件缺失或损坏，无法恢复")
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    if hasher.hexdigest() != digest:
        raise ApiError(409, "trash_asset_unavailable", "资料原件缺失或损坏，无法恢复")


def _verify_originals(db: Session, source: Source) -> None:
    asset = db.scalar(select(FileAsset).where(FileAsset.source_id == source.id))
    if source.kind == "file" and asset is None:
        raise ApiError(409, "trash_asset_unavailable", "资料原件缺失，无法恢复")
    if asset:
        verify_asset(asset.id, asset.size_bytes, asset.sha256)
    for image in db.scalars(select(SourceImage).where(SourceImage.source_id == source.id)):
        if image.status == "saved":
            if not image.file_key:
                raise ApiError(409, "trash_asset_unavailable", "资料图片缺失，无法恢复")
            verify_asset("images/" + image.file_key, image.size_bytes, image.sha256)


def verify_originals(db: Session, source: Source) -> None:
    try:
        _verify_originals(db, source)
    except OSError as exc:
        raise ApiError(409, "trash_asset_unavailable", "资料原件不可读取，无法恢复") from exc


@router.get("")
def list_trash(
    limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
    principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db),
) -> dict:
    sources = db.scalars(select(Source).where(
        Source.owner_id == principal.owner_id, Source.deleted_at.is_not(None),
        Source.processing_status != "deleted").order_by(Source.deleted_at.desc(), Source.id)
        .offset(offset).limit(limit + 1)).all()
    items = []
    for source in sources[:limit]:
        job = db.scalar(select(DeletionJob).where(DeletionJob.source_id == source.id)
                        .order_by(DeletionJob.created_at.desc(), DeletionJob.id.desc()).limit(1))
        blocked = db.scalar(select(DeletionJob.id).where(
            DeletionJob.source_id == source.id, DeletionJob.status.in_(CLEANUP_STATUSES)).limit(1))
        domain_trashed = db.get(DomainTrash, source.domain_id) is not None
        items.append({"id": source.id, "title": source.title, "deleted_at": source.deleted_at,
                      "content_type": content_type(source),
                      "can_restore": not domain_trashed and source.processing_status != "deleting" and blocked is None,
                      "purge_status": "domain_trashed" if domain_trashed and blocked is None else (job.status if job else "trashed")})
    return {"items": items, "next_offset": offset + limit if len(sources) > limit else None}


@router.post("/{source_id}/restore")
def restore_source(
    source_id: str, principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    return restore_source_record(db, principal.owner_id, source_id)


def restore_source_record(db: Session, owner_id: str, source_id: str, *, commit: bool = True) -> dict:
    # The source write acquires SQLite's write lock before reading task state or files.
    blocked = exists().where(DeletionJob.source_id == Source.id,
                             DeletionJob.status.in_(CLEANUP_STATUSES))
    changed = db.execute(update(Source).where(
        Source.id == source_id, Source.owner_id == owner_id,
        Source.deleted_at.is_not(None), Source.processing_status.not_in(("deleting", "deleted")),
        ~blocked).values(updated_at=utcnow()).execution_options(synchronize_session=False)).rowcount
    if not changed:
        trash_source(db, owner_id, source_id)
        raise ApiError(409, "trash_not_restorable", "资料正在永久清理或已无法恢复")
    db.expire_all()
    source = trash_source(db, owner_id, source_id)
    if db.get(DomainTrash, source.domain_id) is not None:
        raise ApiError(409, "domain_trashed", "请先在回收站恢复所属知识库")
    verify_originals(db, source)
    source.deleted_at = None
    source.processing_status = "usable" if source.current_revision_id else "failed"
    db.execute(update(DeletionJob).where(DeletionJob.source_id == source.id,
               DeletionJob.status == "trashed").values(status="cancelled", completed_at=utcnow()))
    for model in (DocumentJob, OCRJob, WebReadJob):
        db.execute(update(model).where(model.source_id == source.id, model.status == "cancelled")
                   .values(status="failed", last_error="资料曾移入回收站，请手动重试处理。", completed_at=utcnow()))
    db.flush()
    reindex_source(db, source)
    if commit:
        db.commit()
    return {"id": source.id, "status": "restored"}


@router.delete("/{source_id}", status_code=202)
def purge_source(
    source_id: str, principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    changed = db.execute(update(Source).where(
        Source.id == source_id, Source.owner_id == principal.owner_id,
        Source.deleted_at.is_not(None), Source.processing_status != "deleted")
        .values(processing_status="deleting").execution_options(synchronize_session=False)).rowcount
    if not changed:
        raise ApiError(404, "not_found", "回收站资料不存在")
    job = db.scalar(select(DeletionJob).where(DeletionJob.source_id == source_id,
                    DeletionJob.status.in_(("trashed", "pending", "retry", "running", "failed")))
                    .order_by(DeletionJob.created_at.desc(), DeletionJob.id.desc()).limit(1))
    if job is None:
        job = DeletionJob(owner_id=principal.owner_id, source_id=source_id, status="pending")
        db.add(job)
    elif job.status in {"trashed", "failed"}:
        job.status = "pending"
        job.attempts = 0
        job.last_error = None
        job.completed_at = None
    db.commit()
    db.refresh(job)
    return {"operation_id": job.id, "status": job.status}
