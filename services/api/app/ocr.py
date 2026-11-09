"""Account OCR preferences and PDF reprocessing requests."""
from fastapi import APIRouter, Depends
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import AppPrincipal, require_app_principal
from .config import settings
from .database import get_db
from .errors import ApiError
from .models import FileAsset, OCRJob, OCRPreference
from .services import require_source

router = APIRouter()
OCRModel = Literal["tiny", "small", "medium"]
OCREngine = Literal["pdfium", "pdf_inspector"]


class OCRPreferenceUpdate(BaseModel):
    model: OCRModel
    engine: OCREngine | None = None


def job_view(job: OCRJob) -> dict:
    return {"id": job.id, "source_id": job.source_id, "model": job.model, "engine": job.engine,
            "status": job.status, "attempts": job.attempts,
            "last_error": job.last_error, "page_count": job.page_count,
            "created_at": job.created_at, "completed_at": job.completed_at}


@router.get("/v1/ocr/preferences")
def get_preference(principal: AppPrincipal = Depends(require_app_principal),
                   db: Session = Depends(get_db)) -> dict:
    preference = db.get(OCRPreference, principal.owner_id)
    return {"model": preference.model if preference else "small",
            "engine": preference.engine if preference else "pdfium"}


@router.put("/v1/ocr/preferences")
def set_preference(payload: OCRPreferenceUpdate,
                   principal: AppPrincipal = Depends(require_app_principal),
                   db: Session = Depends(get_db)) -> dict:
    preference = db.get(OCRPreference, principal.owner_id)
    if preference is None:
        preference = OCRPreference(owner_id=principal.owner_id, model=payload.model,
                                   engine=payload.engine or "pdfium")
        db.add(preference)
    else:
        preference.model = payload.model
        if payload.engine is not None:
            preference.engine = payload.engine
    db.commit()
    return {"model": preference.model, "engine": preference.engine}


@router.post("/v1/sources/{source_id}/ocr", status_code=202)
def reprocess_pdf(source_id: str, principal: AppPrincipal = Depends(require_app_principal),
                  db: Session = Depends(get_db)) -> dict:
    source = require_source(db, principal.owner_id, source_id)
    asset = db.scalar(select(FileAsset).where(FileAsset.source_id == source.id))
    if asset is None:
        raise ApiError(422, "not_pdf", "该资料不是 PDF 文件")
    path = settings.upload_dir / asset.id
    if not path.is_file():
        raise ApiError(422, "not_pdf", "该资料不是 PDF 文件")
    with path.open("rb") as uploaded:
        is_pdf = uploaded.read(5) == b"%PDF-"
    if not is_pdf:
        raise ApiError(422, "not_pdf", "该资料不是 PDF 文件")
    active = db.scalar(select(OCRJob).where(OCRJob.source_id == source.id,
                                           OCRJob.status.in_(["pending", "running"])))
    if active:
        raise ApiError(409, "ocr_in_progress", "该 PDF 已有识别任务在进行")
    preference = db.get(OCRPreference, principal.owner_id)
    job = OCRJob(owner_id=principal.owner_id, source_id=source.id,
                 model=preference.model if preference else "small",
                 engine=preference.engine if preference else "pdfium")
    db.add(job)
    source.processing_status = "pending"
    db.commit()
    db.refresh(job)
    return job_view(job)
