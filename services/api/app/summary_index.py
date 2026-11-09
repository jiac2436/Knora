"""Disposable local indexes for version-bound AI document summaries."""
from __future__ import annotations

import json
import re
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from . import semantic
from .ai import is_stale, visible_source
from .ai_contract import AnalysisRequest, AnalysisResult, validate_result
from .auth import AppPrincipal, require_app_principal
from .database import get_db
from .models import (AIArtifact, EvidenceChunk, EvidenceEmbedding, SemanticIndexJob, Source,
                     SummaryEmbedding, SummaryIndexJob, SummarySearchTerm)

router = APIRouter(prefix="/v1/summary-index")


def current_summary_artifact(db: Session, source: Source, *, candidates=None, current_context=None) -> AIArtifact | None:
    if source.deleted_at is not None or source.processing_status in {"deleting", "deleted"}:
        return None
    artifacts = candidates if candidates is not None else db.scalars(select(AIArtifact).where(
        AIArtifact.source_id == source.id, AIArtifact.owner_id == source.owner_id,
        AIArtifact.revision_id == source.current_revision_id,
    ).order_by(AIArtifact.created_at.desc(), AIArtifact.id.desc()))
    for artifact in artifacts:
        try:
            if (artifact.source_id, artifact.owner_id, artifact.revision_id) != (source.id, source.owner_id, source.current_revision_id):
                continue
            snapshot = json.loads(artifact.snapshot_json)
            wire = snapshot.get("analysis_request", snapshot)
            if snapshot.get("phase", "single") == "segment" or wire.get("phase", "single") == "segment":
                continue
            if is_stale(db, source, snapshot, current_context=current_context):
                continue
            request = AnalysisRequest.model_validate({k: v for k, v in wire.items() if k in AnalysisRequest.model_fields})
            if request.source_id != source.id or request.revision_id != source.current_revision_id:
                continue
            validate_result(request, AnalysisResult.model_validate_json(artifact.result_json))
            return artifact
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return None


def summary_text(artifact: AIArtifact) -> str:
    result = AnalysisResult.model_validate_json(artifact.result_json)
    parts = ([result.overview.text] if result.overview else []) + [result.summary]
    parts.extend(item.text for item in result.insights)
    parts.extend(item.text for item in result.limitations)
    return "\n".join(parts)


def _terms(text: str) -> set[str]:
    from .services import _query_tokens
    terms = {term for term in _query_tokens(text) if len(term) <= 128}
    for match in re.finditer(r"[\u3400-\u9fff]+", text.casefold()):
        terms.update(match.group())
    terms.update(token[:length] for token in tuple(terms)
                 if token.isascii() and len(token) > 2
                 for length in range(2, min(len(token), 32) + 1))
    return terms


def _purge_source(db: Session, source_id: str) -> None:
    db.execute(delete(SummarySearchTerm).where(SummarySearchTerm.source_id == source_id))
    db.execute(delete(SummaryEmbedding).where(SummaryEmbedding.source_id == source_id))


def _complete_data(db: Session, artifact: AIArtifact, text: str) -> bool:
    terms = set(db.scalars(select(SummarySearchTerm.term).where(
        SummarySearchTerm.artifact_id == artifact.id, SummarySearchTerm.owner_id == artifact.owner_id,
        SummarySearchTerm.source_id == artifact.source_id)))
    if terms != _terms(text):
        return False
    if semantic.settings.semantic_enabled:
        embeddings = db.scalars(select(SummaryEmbedding).where(
            SummaryEmbedding.artifact_id == artifact.id, SummaryEmbedding.model == semantic.MODEL_NAME,
            SummaryEmbedding.owner_id == artifact.owner_id, SummaryEmbedding.source_id == artifact.source_id,
            SummaryEmbedding.revision_id == artifact.revision_id
        ).order_by(SummaryEmbedding.ordinal)).all()
        spans = [(start, end) for start, end, _ in semantic.windows(text)]
        if [(row.start, row.end) for row in embeddings] != spans:
            return False
    return True


def artifact_index_status(db: Session, artifact: AIArtifact) -> str:
    """Readiness of one artifact, never another historical summary's index."""
    job = db.get(SummaryIndexJob, artifact.source_id)
    if job is None or job.artifact_id != artifact.id:
        return "pending"
    if job.status == "failed":
        return "failed"
    if job.status != "completed":
        return "pending"
    return "ready" if _complete_data(db, artifact, summary_text(artifact)) else "pending"


def source_semantic_index_status(db: Session, source: Source) -> str:
    if not semantic.settings.semantic_enabled:
        return "disabled"
    job = db.get(SemanticIndexJob, source.id)
    if job is None or job.revision_id != source.current_revision_id:
        return "pending"
    if job.status == "failed":
        return "failed"
    if job.status != "completed":
        return "pending"
    evidence = db.scalars(select(EvidenceChunk).where(
        EvidenceChunk.revision_id == source.current_revision_id)).all()
    expected = sorted((item.id, start, end) for item in evidence
                      for start, end, _ in semantic.windows(item.text))
    actual = sorted(db.execute(select(EvidenceEmbedding.evidence_id, EvidenceEmbedding.start,
                                     EvidenceEmbedding.end).where(
        EvidenceEmbedding.owner_id == source.owner_id, EvidenceEmbedding.source_id == source.id,
        EvidenceEmbedding.revision_id == source.current_revision_id,
        EvidenceEmbedding.model == semantic.MODEL_NAME)).all())
    return "ready" if actual == expected else "pending"


def enqueue_summary_index(db: Session, source: Source) -> None:
    artifact = current_summary_artifact(db, source)
    job = db.get(SummaryIndexJob, source.id)
    if artifact is None:
        _purge_source(db, source.id)
        if job is not None:
            db.delete(job)
        return
    if job is None:
        db.add(SummaryIndexJob(source_id=source.id, artifact_id=artifact.id, status="pending"))
        return
    if job.artifact_id != artifact.id:
        _purge_source(db, source.id)
        job.artifact_id, job.status, job.attempts, job.last_error = artifact.id, "pending", 0, None
    elif job.status == "completed" and not _complete_data(db, artifact, summary_text(artifact)):
        job.status, job.last_error = "pending", None


def ensure_summary_index(db: Session) -> None:
    for source in db.scalars(select(Source).order_by(Source.id)):
        enqueue_summary_index(db, source)
    db.commit()


def index_summary_once(db: Session) -> bool:
    job = db.scalar(select(SummaryIndexJob).where(SummaryIndexJob.status == "pending")
                    .order_by(SummaryIndexJob.source_id).limit(1))
    if job is None:
        return False
    source_id, artifact_id = job.source_id, job.artifact_id
    claimed = db.execute(update(SummaryIndexJob).where(
        SummaryIndexJob.source_id == source_id, SummaryIndexJob.status == "pending",
        SummaryIndexJob.artifact_id == artifact_id,
    ).values(status="running", attempts=SummaryIndexJob.attempts + 1)).rowcount
    db.commit()
    if not claimed:
        return True
    try:
        db.execute(update(Source).where(Source.id == source_id).values(updated_at=Source.updated_at))
        db.expire_all()
        source = db.get(Source, source_id)
        artifact = current_summary_artifact(db, source) if source else None
        job = db.get(SummaryIndexJob, source_id)
        if artifact is None or artifact.id != artifact_id or job is None or job.artifact_id != artifact_id:
            if source:
                enqueue_summary_index(db, source)
            db.commit()
            return True
        text, title = summary_text(artifact), source.title
        owner_id, revision_id = source.owner_id, source.current_revision_id
        _purge_source(db, source_id)
        db.add_all(SummarySearchTerm(artifact_id=artifact_id, term=term, owner_id=owner_id, source_id=source_id)
                   for term in sorted(_terms(text)))
        # Persist keyword search before local model inference; inference failure must not hide summaries.
        db.commit()
        parts = list(semantic.windows(text))
        vectors = semantic.embed_documents([title + " " + part[2] for part in parts]) if semantic.settings.semantic_enabled else []
        db.execute(update(Source).where(Source.id == source_id).values(updated_at=Source.updated_at))
        db.expire_all()
        source = db.get(Source, source_id)
        job = db.get(SummaryIndexJob, source_id)
        latest = current_summary_artifact(db, source) if source else None
        if source is None or latest is None or latest.id != artifact_id or job is None or job.artifact_id != artifact_id or job.status != "running":
            if source:
                enqueue_summary_index(db, source)
            db.commit()
            return True
        db.execute(delete(SummaryEmbedding).where(SummaryEmbedding.source_id == source_id))
        if semantic.settings.semantic_enabled:
            for ordinal, ((start, end, _), vector) in enumerate(zip(parts, vectors, strict=True)):
                db.add(SummaryEmbedding(artifact_id=artifact_id, ordinal=ordinal, owner_id=owner_id,
                    source_id=source_id, revision_id=revision_id, start=start, end=end,
                    model=semantic.MODEL_NAME, vector=vector))
        job.status, job.last_error = "completed", None
        db.commit()
    except Exception as exc:
        db.rollback()
        job = db.get(SummaryIndexJob, source_id)
        if job is not None and job.artifact_id == artifact_id and job.status == "running":
            job.status, job.last_error = "failed", type(exc).__name__
            db.commit()
        raise
    return True


def summary_index_status(db: Session, owner_id: str) -> str:
    statuses = []
    for source in db.scalars(select(Source).where(Source.owner_id == owner_id, Source.deleted_at.is_(None),
                                                Source.processing_status.not_in(("deleting", "deleted")))):
        artifact = current_summary_artifact(db, source)
        if artifact is None:
            continue
        job = db.get(SummaryIndexJob, source.id)
        if job is None or job.artifact_id != artifact.id:
            statuses.append("pending")
        elif job.status in {"pending", "running"}:
            statuses.append("pending")
        elif _complete_data(db, artifact, summary_text(artifact)):
            statuses.append("ready")
        else:
            statuses.append("failed" if job.status == "failed" else "pending")
    if "failed" in statuses:
        return "failed"
    return "pending" if "pending" in statuses else "ready"


def rebuild_summary_index(db: Session, owner_id: str, source_id: str | None = None) -> dict:
    sources = [visible_source(db, owner_id, source_id)] if source_id else list(db.scalars(
        select(Source).where(Source.owner_id == owner_id, Source.deleted_at.is_(None),
                             Source.processing_status.not_in(("deleting", "deleted")))))
    count = 0
    for source in sources:
        db.execute(update(Source).where(Source.id == source.id).values(updated_at=Source.updated_at))
        db.expire_all()
        source = db.get(Source, source.id)
        enqueue_summary_index(db, source)
        job = db.get(SummaryIndexJob, source.id)
        if job is not None:
            job.status, job.last_error = "pending", None
            count += 1
            # Retry the local retrieval pipeline without creating any paid AI job.
            if source_semantic_index_status(db, source) in {"pending", "failed"}:
                semantic.enqueue_semantic_index(db, source)
                semantic_job = db.get(SemanticIndexJob, source.id)
                if semantic_job is not None:
                    semantic_job.status, semantic_job.last_error = "pending", None
    db.commit()
    return {"queued": count, "status": summary_index_status(db, owner_id)}


class RebuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: UUID | None = None


@router.get("")
def index_status(principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    return {"status": summary_index_status(db, principal.owner_id), "semantic_enabled": semantic.settings.semantic_enabled}


@router.post("/rebuild")
def rebuild(body: RebuildRequest, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    return rebuild_summary_index(db, principal.owner_id, str(body.source_id) if body.source_id else None)
