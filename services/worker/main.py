from __future__ import annotations

import argparse
import logging
import os
import time

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.orm import Session

from app.database import SessionLocal, create_schema
from app.config import settings
from app.errors import ApiError
from app.runtime_extensions import ExtensionRequired, available
from app.models import (
    AIArtifact,
    AIJob,
    AIJobStep,
    DeletionJob,
    DocumentJob,
    EvidenceChunk,
    ExperienceRecord,
    FileAsset,
    OCRJob,
    PersonalContext,
    Source,
    SourceProject,
    SourceTag,
    SourceTagState,
    TagAssignment,
    TagSuggestion,
    TagRejection,
    SourceSearchTerm,
    SourceSearchDocument,
    SemanticIndexJob,
    SummaryIndexJob,
    SummarySearchTerm,
    SummaryEmbedding,
    EvidenceEmbedding,
    SourceRevision,
    WebReadJob,
    WebReadMarkdown,
    FeishuDocumentArtifact,
    SourceImage,
    utcnow,
)
from .pdf_ocr import extract_pdf, extract_pdf_inspector
from .anydoc_reader import extract_document
from .web_reader import (WECHAT_VERIFICATION_ERROR, _is_wechat_verification,
                         extract_browser_page_with_markdown, extract_legacy_page,
                         extract_wechat_page, is_wechat_url)
from app.services import reindex_source
from app.semantic import ensure_semantic_index, index_one
from app.summary_index import ensure_summary_index, index_summary_once
from app.domain_runtime import interrupt_domain_processing
from services.worker.domain_summary import run_node_once, interrupt_node_jobs
from services.worker.domain_build import run_build_once, interrupt_builds
from services.worker.domain_full_analysis import run_full_analysis_once, interrupt_full_analysis
from app.domain_schedule import schedule_domain_once
from app.domain_node_index import ensure_node_index, index_node_once, interrupt_node_index
from app.domain_topics import ensure_local_topics, prepare_local_topics_once
from .ai_analysis import run_ai_once
from .rag_answer import run_rag_once, interrupt_rag
from .web_images import run_image_once
from app.web_images import BROWSER_IMAGE_METHOD, enqueue_images, WEB_IMAGE_METHOD

logger = logging.getLogger(__name__)


def claim_local_result(db: Session, job, expected_status: str = "running") -> bool:
    """Hold the source write lock while claiming or publishing a local job."""
    if getattr(settings, "environment", "development") != "local":
        return True
    model = type(job)
    active = exists().where(model.id == job.id, model.source_id == Source.id,
                            model.status == expected_status)
    changed = db.execute(update(Source).where(
        Source.id == job.source_id, Source.deleted_at.is_(None), active)
        .values(updated_at=utcnow()).execution_options(synchronize_session=False)).rowcount
    if not changed:
        db.rollback()
        return False
    db.refresh(job)
    return True


def deletion_asset_path(relative: str):
    if getattr(settings, "environment", "development") != "local":
        return settings.upload_dir / relative
    parts = relative.split("/")
    if "\\" in relative or "\x00" in relative or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("资料原件路径无效")
    if any(path.is_symlink() for path in (settings.upload_dir, *settings.upload_dir.parents)):
        raise ValueError("资料原件目录不可用")
    path = settings.upload_dir.resolve()
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("资料原件路径不可用")
    return path


def extension_wait_exclusions(model):
    # Persist the existing pending state for downgrade compatibility. Waiting is
    # derived from the error code; unavailable work must not spin or block peers.
    return [or_(model.last_error.is_(None), ~model.last_error.like(f"extension_required:{key}:%"))
            for key in ("documents", "ocr") if not available(key)]


def run_document_once(db: Session, extractor=extract_document) -> bool:
    job = db.scalar(select(DocumentJob).where(DocumentJob.status == "pending", *extension_wait_exclusions(DocumentJob))
                    .order_by(DocumentJob.created_at, DocumentJob.id).limit(1))
    if job is None:
        return False
    if not claim_local_result(db, job, expected_status="pending"):
        return True
    source = db.get(Source, job.source_id)
    if source is None or source.deleted_at is not None:
        job.status = "cancelled"
        job.completed_at = utcnow()
        db.commit()
        return True
    job.status = "running"
    job.attempts += 1
    source.processing_status = "processing"
    db.commit()
    try:
        asset = db.scalar(select(FileAsset).where(FileAsset.source_id == job.source_id))
        if asset is None:
            raise ValueError("原文件不存在")
        text = extractor(settings.upload_dir / asset.id, asset.filename)
        if not claim_local_result(db, job):
            return True
        db.refresh(source)
        if source.deleted_at is not None:
            job.status = "cancelled"
            job.completed_at = utcnow()
            db.commit()
            return True
        current = db.get(SourceRevision, source.current_revision_id) if source.current_revision_id else None
        supplement = ""
        if current and current.parser_version == "anydoc-supplement-v1":
            prior = db.scalar(select(SourceRevision).where(
                SourceRevision.source_id == source.id, SourceRevision.parser_version == "anydoc-v1"
            ).order_by(SourceRevision.version.desc()).limit(1))
            if prior and current.extracted_text.startswith(prior.extracted_text):
                supplement = current.extracted_text[len(prior.extracted_text):].strip()
            elif prior is None:
                supplement = current.extracted_text.strip()
        combined = "\n\n".join(part for part in (text, supplement) if part)
        version = db.scalar(select(func.max(SourceRevision.version))
                            .where(SourceRevision.source_id == source.id)) or 0
        revision = SourceRevision(source_id=source.id, version=version + 1,
                                  extracted_text=combined, parser_version="anydoc-v1")
        db.add(revision)
        db.flush()
        db.add(EvidenceChunk(revision_id=revision.id, ordinal=0, text=text,
                             locator_kind="text", locator_start=0, locator_end=len(text)))
        if supplement:
            db.add(EvidenceChunk(revision_id=revision.id, ordinal=1, text=supplement,
                                 locator_kind="text", locator_start=len(combined) - len(supplement),
                                 locator_end=len(combined)))
        source.current_revision_id = revision.id
        source.coverage = "partial_text" if supplement else "full_text"
        source.processing_status = "usable"
        job.status = "completed"
        job.last_error = None
        job.completed_at = utcnow()
        db.flush()
        reindex_source(db, source)
        db.commit()
        logger.info("文档解析完成：任务 %s", job.id)
    except Exception as exc:
        db.rollback()
        failed = db.get(DocumentJob, job.id)
        source = db.get(Source, job.source_id)
        if getattr(settings, "environment", "development") == "local":
            if failed is None or not claim_local_result(db, failed):
                return True
            if source:
                db.refresh(source)
        if failed:
            failed.status = "pending" if isinstance(exc, ExtensionRequired) else "failed"
            failed.last_error = (f"extension_required:{exc.capability}:" + str(exc) if isinstance(exc, ExtensionRequired)
                                 else str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
            failed.completed_at = None if isinstance(exc, ExtensionRequired) else utcnow()
        if source and source.deleted_at is None:
            source.processing_status = ("usable" if source.current_revision_id else
                                        "pending" if isinstance(exc, ExtensionRequired) else "failed")
        db.commit()
        logger.error("文档解析失败：任务 %s，错误 %s", job.id,
                     failed.last_error if failed else type(exc).__name__)
    return True


def run_web_once(db: Session, extractor=None) -> bool:
    job = db.scalar(select(WebReadJob).where(WebReadJob.status == "pending")
                    .order_by(WebReadJob.created_at, WebReadJob.id).limit(1))
    if job is None:
        return False
    if not claim_local_result(db, job, expected_status="pending"):
        return True
    source = db.get(Source, job.source_id)
    if source is None or source.deleted_at is not None:
        job.status = "cancelled"
        job.completed_at = utcnow()
        db.commit()
        return True
    job.status = "running"
    job.attempts += 1
    source.processing_status = "processing"
    db.commit()
    try:
        if not source.raw_url:
            raise ValueError("原始网址不存在")
        markdown = None
        if extractor is not None:
            title, text = extractor(source.raw_url)
        elif is_wechat_url(source.raw_url):
            title, text, markdown = extract_wechat_page(source.raw_url)
        elif job.engine == "crawl4ai":
            title, text, markdown = extract_browser_page_with_markdown(
                source.raw_url, cleaning_enabled=job.cleaning_enabled)
            if _is_wechat_verification(source.raw_url, title, text):
                raise ValueError(WECHAT_VERIFICATION_ERROR)
        else:
            title, text = extract_legacy_page(source.raw_url)
        if not claim_local_result(db, job):
            return True
        db.refresh(source)
        if source is None or source.deleted_at is not None:
            job.status = "cancelled"
            job.completed_at = utcnow()
            db.commit()
            return True
        supplement = ""
        current = db.get(SourceRevision, source.current_revision_id) if source.current_revision_id else None
        is_wechat = is_wechat_url(source.raw_url)
        if current and current.parser_version in {"web-supplement-v1", "web-html-v1", "web-crawl4ai-v1", "web-wechat-v1"}:
            prior_web = current if current.parser_version in {"web-html-v1", "web-crawl4ai-v1", "web-wechat-v1"} else db.scalar(
                select(SourceRevision).where(SourceRevision.source_id == source.id,
                                             SourceRevision.parser_version.in_(["web-html-v1", "web-crawl4ai-v1", "web-wechat-v1"]))
                .order_by(SourceRevision.version.desc()).limit(1))
            prior_supplement = db.scalar(select(EvidenceChunk.text).where(
                EvidenceChunk.revision_id == prior_web.id, EvidenceChunk.ordinal == 1)) if prior_web else None
            base = prior_web.extracted_text if prior_web else source.raw_url or ""
            added = current.extracted_text[len(base):].strip() if current.extracted_text.startswith(base) else ""
            supplement = "\n\n".join(part for part in [prior_supplement, added] if part)
        combined = "\n\n".join([text, supplement]) if supplement else text
        version = db.scalar(select(func.max(SourceRevision.version))
                            .where(SourceRevision.source_id == source.id)) or 0
        parser_ver = "web-wechat-v1" if is_wechat else ("web-crawl4ai-v1" if markdown is not None else "web-html-v1")
        revision = SourceRevision(source_id=source.id, version=version + 1,
                                  extracted_text=combined,
                                  parser_version=parser_ver)
        db.add(revision)
        db.flush()
        if markdown is not None:
            db.add(WebReadMarkdown(revision_id=revision.id, markdown=markdown))
            if is_wechat:
                enqueue_images(db, source, revision, markdown)
        db.add(EvidenceChunk(revision_id=revision.id, ordinal=0, text=text,
                             locator_kind="text", locator_start=0, locator_end=len(text)))
        if supplement:
            db.add(EvidenceChunk(revision_id=revision.id, ordinal=1, text=supplement,
                                 locator_kind="text", locator_start=len(text) + 2,
                                 locator_end=len(combined)))
        source.current_revision_id = revision.id
        source.title = title
        source.coverage = "partial_text" if supplement else "full_text"
        source.processing_status = "usable"
        job.status = "completed"
        job.last_error = None
        job.completed_at = utcnow()
        db.flush()
        reindex_source(db, source)
        db.commit()
        logger.info("网页读取完成：任务 %s", job.id)
    except Exception as exc:
        db.rollback()
        failed = db.get(WebReadJob, job.id)
        source = db.get(Source, job.source_id)
        if getattr(settings, "environment", "development") == "local":
            if failed is None or not claim_local_result(db, failed):
                return True
            if source:
                db.refresh(source)
        if failed:
            failed.status = "failed"
            failed.last_error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            failed.completed_at = utcnow()
        if source and source.deleted_at is None:
            source.processing_status = "failed"
        db.commit()
        logger.warning("网页读取失败：任务 %s，错误 %s", job.id,
                       failed.last_error if failed else type(exc).__name__)
    return True


def run_ocr_once(db: Session, extractor=None) -> bool:
    job = db.scalar(select(OCRJob).where(OCRJob.status == "pending", *extension_wait_exclusions(OCRJob))
                    .order_by(OCRJob.created_at, OCRJob.id).limit(1))
    if job is None:
        return False
    if not claim_local_result(db, job, expected_status="pending"):
        return True
    source = db.get(Source, job.source_id)
    if source is None or source.deleted_at is not None:
        job.status = "cancelled"
        job.completed_at = utcnow()
        db.commit()
        return True
    job.status = "running"
    job.attempts += 1
    source.processing_status = "processing"
    db.commit()
    logger.info("PDF 识别开始：任务 %s，引擎 %s，模型 %s", job.id, job.engine, job.model)
    try:
        asset = db.scalar(select(FileAsset).where(FileAsset.source_id == job.source_id))
        if asset is None:
            raise ValueError("原 PDF 文件不存在")
        path = settings.upload_dir / asset.id
        selected_extractor = extractor or {
            "pdfium": extract_pdf,
            "pdf_inspector": extract_pdf_inspector,
        }.get(job.engine)
        if selected_extractor is None:
            raise ValueError("不支持的 PDF 识别引擎")
        pending_extension = None
        try:
            pages = selected_extractor(path, job.model)
        except ExtensionRequired as blocked:
            if not hasattr(blocked, "partial_pages"):
                raise
            pages = blocked.partial_pages
            pending_extension = blocked
        if not claim_local_result(db, job):
            return True
        source = db.get(Source, job.source_id)
        if source:
            db.refresh(source)
        if source is None or source.deleted_at is not None:
            job.status = "cancelled"
            job.completed_at = utcnow()
            db.commit()
            return True
        text_pages = [(number, text.strip()) for number, text in pages if text.strip()]
        supplement = ""
        current = db.get(SourceRevision, source.current_revision_id) if source.current_revision_id else None
        if current and current.parser_version == "pdf-supplement-v1":
            prior_ocr = db.scalar(select(SourceRevision)
                                  .where(SourceRevision.source_id == source.id,
                                         or_(SourceRevision.parser_version.like("pdf-ppocrv6-%"),
                                             SourceRevision.parser_version.like("pdf-inspector-ppocrv6-%")))
                                  .order_by(SourceRevision.version.desc()).limit(1))
            if prior_ocr and current.extracted_text.startswith(prior_ocr.extracted_text):
                supplement = current.extracted_text[len(prior_ocr.extracted_text):].strip()
            elif prior_ocr is None:
                supplement = current.extracted_text.strip()
        if text_pages or supplement:
            version = db.scalar(select(func.max(SourceRevision.version))
                                .where(SourceRevision.source_id == source.id)) or 0
            combined = "\n\n".join([text for _, text in text_pages] + ([supplement] if supplement else []))
            revision = SourceRevision(source_id=source.id, version=version + 1,
                                      extracted_text=combined,
                                      parser_version=(f"pdf-ppocrv6-{job.model}-v1" if job.engine == "pdfium"
                                                      else f"pdf-inspector-ppocrv6-{job.model}-v1"))
            db.add(revision)
            db.flush()
            for ordinal, (number, text) in enumerate(text_pages):
                db.add(EvidenceChunk(revision_id=revision.id, ordinal=ordinal, text=text,
                                     locator_kind="pdf_page", locator_start=number, locator_end=number))
            if supplement:
                db.add(EvidenceChunk(revision_id=revision.id, ordinal=len(text_pages),
                                     text=supplement, locator_kind="text",
                                     locator_start=len(combined) - len(supplement), locator_end=len(combined)))
            source.current_revision_id = revision.id
            source.coverage = "partial_text" if supplement or len(text_pages) < len(pages) else "full_text"
            source.processing_status = "usable"
            db.flush()
            reindex_source(db, source)
        else:
            if current is not None:
                source.processing_status = "usable"
            else:
                source.processing_status = "needs_input"
        job.status = "pending" if pending_extension else "completed"
        job.page_count = len(pages)
        job.last_error = (f"extension_required:{pending_extension.capability}:" + str(pending_extension)
                          if pending_extension else None)
        if pending_extension and source.current_revision_id is None:
            source.processing_status = "pending"
        job.completed_at = None if pending_extension else utcnow()
        db.commit()
        logger.info("PDF 识别完成：任务 %s，共 %s 页", job.id, job.page_count)
    except Exception as exc:
        db.rollback()
        failed = db.get(OCRJob, job.id)
        source = db.get(Source, job.source_id)
        if getattr(settings, "environment", "development") == "local":
            if failed is None or not claim_local_result(db, failed):
                return True
            if source:
                db.refresh(source)
        if failed:
            failed.status = "pending" if isinstance(exc, ExtensionRequired) else "failed"
            failed.last_error = (f"extension_required:{exc.capability}:" + str(exc) if isinstance(exc, ExtensionRequired)
                                 else str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
            failed.completed_at = None if isinstance(exc, ExtensionRequired) else utcnow()
        if source and source.deleted_at is None:
            source.processing_status = ("usable" if source.current_revision_id else
                                        "pending" if isinstance(exc, ExtensionRequired) else "failed")
        db.commit()
        logger.error("PDF 识别失败：任务 %s，错误 %s", job.id, failed.last_error if failed else type(exc).__name__)
    return True


def run_once(db: Session) -> bool:
    job = db.scalar(
        select(DeletionJob)
        .where(DeletionJob.status.in_(["pending", "retry"]))
        .order_by(DeletionJob.created_at)
        .limit(1)
    )
    if job is None:
        from .capture import run_capture_once
        if run_capture_once(db):
            return True
        if run_ocr_once(db):
            return True
        if run_document_once(db):
            return True
        if run_web_once(db):
            return True
        if run_full_analysis_once(db):
            return True
        if run_ai_once(db):
            return True
        if run_build_once(db):
            return True
        if run_node_once(db):
            return True
        if run_rag_once(db):
            return True
        if run_image_once(db):
            return True
        try:
            if prepare_local_topics_once(db):
                return True
            if schedule_domain_once(db):
                return True
            ensure_node_index(db)
            if index_node_once(db):
                return True
            ensure_summary_index(db)
            if index_summary_once(db):
                return True
            return index_one(db)
        except Exception:
            logger.exception("本地主题或语义索引失败；关键词检索仍可使用")
            return True
    # Serialize deletion against local restore through the source row first.
    if getattr(settings, "environment", "development") == "local":
        claimed = db.execute(update(Source).where(
            Source.id == job.source_id, Source.deleted_at.is_not(None),
            Source.processing_status == "deleting")
            .values(updated_at=utcnow()).execution_options(synchronize_session=False)).rowcount
        db.refresh(job)
        if not claimed or job.status not in {"pending", "retry"}:
            db.rollback()
            return False
    job.status = "running"
    job.attempts += 1
    db.commit()
    try:
        db.execute(delete(SummarySearchTerm).where(SummarySearchTerm.source_id == job.source_id))
        db.execute(delete(SummaryEmbedding).where(SummaryEmbedding.source_id == job.source_id))
        db.execute(delete(SummaryIndexJob).where(SummaryIndexJob.source_id == job.source_id))
        from app.domain_summary_jobs import purge_node_source
        purge_node_source(db, job.source_id)
        from app.rag import purge_source_turns
        purge_source_turns(db, job.source_id)
        db.execute(delete(AIArtifact).where(AIArtifact.source_id == job.source_id))
        db.execute(delete(AIJobStep).where(AIJobStep.job_id.in_(select(AIJob.id).where(AIJob.source_id == job.source_id))))
        db.execute(delete(AIJob).where(AIJob.source_id == job.source_id))
        db.execute(delete(TagAssignment).where(TagAssignment.source_id == job.source_id))
        db.execute(delete(TagSuggestion).where(TagSuggestion.source_id == job.source_id))
        db.execute(delete(TagRejection).where(TagRejection.source_id == job.source_id))
        for image in db.scalars(select(SourceImage).where(SourceImage.source_id == job.source_id)):
            if image.file_key:
                deletion_asset_path("images/" + image.file_key).unlink(missing_ok=True)
            db.delete(image)
        db.flush()
        for asset in db.scalars(select(FileAsset).where(FileAsset.source_id == job.source_id)):
            deletion_asset_path(asset.id).unlink(missing_ok=True)
            db.delete(asset)
        revision_ids = list(
            db.scalars(select(SourceRevision.id).where(SourceRevision.source_id == job.source_id)).all()
        )
        if revision_ids:
            db.execute(delete(EvidenceEmbedding).where(EvidenceEmbedding.source_id == job.source_id))
            db.execute(delete(EvidenceChunk).where(EvidenceChunk.revision_id.in_(revision_ids)))
            db.execute(delete(WebReadMarkdown).where(WebReadMarkdown.revision_id.in_(revision_ids)))
            db.execute(delete(FeishuDocumentArtifact).where(FeishuDocumentArtifact.revision_id.in_(revision_ids)))
        db.execute(delete(SourceRevision).where(SourceRevision.source_id == job.source_id))
        db.execute(delete(OCRJob).where(OCRJob.source_id == job.source_id))
        db.execute(delete(DocumentJob).where(DocumentJob.source_id == job.source_id))
        db.execute(delete(WebReadJob).where(WebReadJob.source_id == job.source_id))
        db.execute(delete(PersonalContext).where(PersonalContext.source_id == job.source_id))
        db.execute(delete(ExperienceRecord).where(ExperienceRecord.source_id == job.source_id))
        db.execute(delete(SourceProject).where(SourceProject.source_id == job.source_id))
        if getattr(settings, "environment", "development") == "local":
            db.execute(delete(SourceTag).where(SourceTag.source_id == job.source_id))
            db.execute(delete(SourceTagState).where(SourceTagState.source_id == job.source_id))
        search_document_ids = select(SourceSearchDocument.id).where(SourceSearchDocument.source_id == job.source_id)
        db.execute(delete(SourceSearchTerm).where(SourceSearchTerm.document_id.in_(search_document_ids)))
        db.execute(delete(SourceSearchDocument).where(SourceSearchDocument.source_id == job.source_id))
        db.execute(delete(SemanticIndexJob).where(SemanticIndexJob.source_id == job.source_id))
        source = db.get(Source, job.source_id)
        if source:
            if settings.environment == 'local':
                from app.capture_store import CaptureStore
                from app.models import IdempotencyRecord
                requests = set(db.scalars(select(IdempotencyRecord.client_request_id).where(
                    IdempotencyRecord.owner_id == source.owner_id, IdempotencyRecord.source_id == source.id)))
                CaptureStore(settings.local_paths.data).purge_source(source.owner_id, source.id, requests)
            source.current_revision_id = None
            source.raw_input = None
            source.raw_url = None
            source.canonical_url = None
            source.title = "已删除资料"
            source.processing_status = "deleted"
        job.status = "completed"
        job.completed_at = utcnow()
        job.last_error = None
        db.commit()
    except Exception as exc:
        db.rollback()
        failed = db.get(DeletionJob, job.id)
        if failed:
            failed.status = "retry" if failed.attempts < 3 else "failed"
            failed.last_error = type(exc).__name__
            db.commit()
        return False
    return True


def run_service() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Deletion, file parsing, web reading and semantic indexing worker")
    parser.add_argument("--once", action="store_true", help="Process at most one pending job")
    parser.add_argument("--retry-semantic", action="store_true", help="Retry failed semantic indexing jobs")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    create_schema()
    if settings.environment == 'local':
        from app.capture_store import CaptureStore
        try:
            CaptureStore(settings.local_paths.data).recover()
        except (ApiError, OSError, ValueError):
            logger.warning('采集执行队列暂不可用，原文件保留，其他本地功能继续启动')
    # This deployment runs one worker; resume tasks interrupted by a hard stop.
    with SessionLocal() as db:
        # A provider may already have charged an interrupted request. Never
        # automatically replay it after a worker restart.
        db.execute(update(AIJobStep).where(AIJobStep.status == "running").values(
            status="unknown", error="服务中断，分段调用结果未知；不会自动重发", completed_at=utcnow()))
        db.execute(update(AIJob).where(AIJob.status == "running").values(
            status="unknown", last_error="服务中断，AI 调用结果未知；请确认后手动重新分析", completed_at=utcnow()))
        interrupt_rag(db)
        interrupt_domain_processing(db)
        interrupt_node_jobs(db)
        interrupt_builds(db)
        interrupt_full_analysis(db)
        interrupt_node_index(db)
        if getattr(settings, "environment", "development") == "local":
            deleting = select(Source.id).where(Source.deleted_at.is_not(None),
                                               Source.processing_status == "deleting")
            db.execute(update(DeletionJob).where(DeletionJob.status == "running",
                       DeletionJob.source_id.in_(deleting)).values(status="retry"))
        db.execute(update(OCRJob).where(OCRJob.status == "running").values(status="pending"))
        db.execute(update(DocumentJob).where(DocumentJob.status == "running").values(status="pending"))
        db.execute(update(WebReadJob).where(WebReadJob.status == "running").values(status="pending"))
        db.execute(update(SourceImage).where(SourceImage.retrieval_method.in_([WEB_IMAGE_METHOD, BROWSER_IMAGE_METHOD]),
                   SourceImage.status == 'running').values(status='pending'))
        db.execute(update(SemanticIndexJob).where(SemanticIndexJob.status == "running").values(status="pending"))
        db.execute(update(SummaryIndexJob).where(SummaryIndexJob.status == "running").values(status="pending"))
        if args.retry_semantic:
            db.execute(update(SemanticIndexJob).where(SemanticIndexJob.status == "failed")
                       .values(status="pending", last_error=None))
        db.commit()
        for model in (DocumentJob, OCRJob):
            for blocked in db.scalars(select(model).where(model.status == "pending", model.last_error.like("extension_required:%"))):
                parts = (blocked.last_error or "").split(":", 2)
                if len(parts) == 3 and parts[0] == "extension_required" and parts[1] in {"documents", "ocr"} and available(parts[1]):
                    blocked.status, blocked.last_error, blocked.completed_at = "pending", None, None
        db.commit()
        ensure_local_topics(db)
        ensure_semantic_index(db)
        ensure_summary_index(db)
        ensure_node_index(db)
    ready_fd = os.getenv("PKA_WORKER_READY_FD")
    if ready_fd is not None:
        from app.platform_support import get_platform
        get_platform().processes.notify_ready(int(ready_fd))
    logger.info("Worker 已启动，等待文件解析、网页读取或删除任务")
    while True:
        with SessionLocal() as db:
            processed = run_once(db)
        if args.once:
            break
        if not processed:
            time.sleep(args.poll_seconds)


def main() -> None:
    from app.local_runtime import service_guard
    from app.embedded_gateway import close_embedded_runtime
    try:
        with service_guard():
            run_service()
    finally:
        close_embedded_runtime()


if __name__ == "__main__":
    main()
