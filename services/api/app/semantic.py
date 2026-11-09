from __future__ import annotations

from functools import lru_cache
import logging
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .models import EvidenceChunk, EvidenceEmbedding, SemanticIndexJob, Source, SourceRevision


MODEL_NAME = "BAAI/bge-small-zh-v1.5"
WINDOW_CHARS = 480
OVERLAP_CHARS = 80
logger = logging.getLogger(__name__)
_search_model_unavailable = False


@lru_cache(maxsize=1)
def _model():
    from fastembed import TextEmbedding

    kwargs = {"model_name": MODEL_NAME, "cache_dir": str(settings.semantic_cache_dir)}
    local_path = (Path(settings.semantic_model_path) if settings.semantic_model_path else
                  settings.semantic_cache_dir / "fast-bge-small-zh-v1.5")
    if (local_path / "model_optimized.onnx").is_file():
        kwargs["specific_model_path"] = str(local_path)
    elif settings.semantic_model_path:
        raise FileNotFoundError(f"语义模型目录无效：{local_path}")
    return TextEmbedding(**kwargs)


def _normalize(vector) -> bytes:
    import numpy as np
    values = np.asarray(vector, dtype=np.float32)
    norm = np.linalg.norm(values)
    if not np.isfinite(norm) or norm == 0:
        raise ValueError("语义模型返回了无效向量")
    return (values / norm).tobytes()


def embed_documents(texts: list[str]) -> list[bytes]:
    return [_normalize(vector) for vector in _model().embed(texts)]


def embed_query(query: str):
    import numpy as np
    return np.frombuffer(_normalize(next(_model().embed([query]))), dtype=np.float32)


def windows(text: str):
    start = 0
    while start < len(text):
        end = min(len(text), start + WINDOW_CHARS)
        if text[start:end].strip():
            yield start, end, text[start:end]
        if end == len(text):
            break
        start = end - OVERLAP_CHARS


def enqueue_semantic_index(db: Session, source: Source) -> None:
    if not settings.semantic_enabled or not source.current_revision_id:
        return
    job = db.get(SemanticIndexJob, source.id)
    if job is None:
        db.add(SemanticIndexJob(source_id=source.id, revision_id=source.current_revision_id))
    else:
        if job.revision_id == source.current_revision_id and job.status in {"pending", "running", "completed"}:
            return
        job.revision_id = source.current_revision_id
        job.status = "pending"
        job.last_error = None


def ensure_semantic_index(db: Session) -> None:
    if not settings.semantic_enabled:
        return
    sources = db.scalars(
        select(Source).outerjoin(SemanticIndexJob, SemanticIndexJob.source_id == Source.id)
        .where(Source.deleted_at.is_(None), Source.current_revision_id.is_not(None),
               SemanticIndexJob.source_id.is_(None))
    ).all()
    for source in sources:
        enqueue_semantic_index(db, source)
    db.commit()


def index_one(db: Session) -> bool:
    job = db.scalar(select(SemanticIndexJob).where(SemanticIndexJob.status == "pending")
                    .order_by(SemanticIndexJob.source_id).limit(1))
    if job is None:
        return False
    source = db.get(Source, job.source_id)
    if source is None or source.deleted_at is not None or source.current_revision_id is None:
        db.delete(job)
        db.commit()
        return True
    revision_id = source.current_revision_id
    source_id = source.id
    job.revision_id = revision_id
    job.status = "running"
    job.attempts += 1
    db.commit()
    logger.info("语义索引开始：资料 %s，版本 %s，模型 %s", source_id, revision_id, MODEL_NAME)
    try:
        evidence = db.scalars(select(EvidenceChunk).where(EvidenceChunk.revision_id == revision_id)
                              .order_by(EvidenceChunk.ordinal)).all()
        context = f"{source.title} "
        parts = [(item.id, start, end, context + passage)
                 for item in evidence for start, end, passage in windows(item.text)]
        db.rollback()  # Release the read transaction before potentially slow model inference.
        vectors = embed_documents([part[3] for part in parts]) if parts else []
        source = db.get(Source, source_id)
        job = db.get(SemanticIndexJob, source_id)
        if source is None or source.deleted_at is not None:
            if job is not None:
                db.delete(job)
            db.commit()
            return True
        if source.current_revision_id != revision_id:
            job.status = "pending"
            job.revision_id = source.current_revision_id
            db.commit()
            return True
        db.execute(delete(EvidenceEmbedding).where(EvidenceEmbedding.source_id == source.id))
        for (evidence_id, start, end, _), vector in zip(parts, vectors, strict=True):
            db.add(EvidenceEmbedding(owner_id=source.owner_id, source_id=source.id,
                                     revision_id=revision_id, evidence_id=evidence_id,
                                     start=start, end=end, model=MODEL_NAME, vector=vector))
        job.status = "completed"
        job.last_error = None
        db.commit()
        logger.info("语义索引完成：资料 %s，片段 %s", source_id, len(parts))
    except Exception as exc:
        db.rollback()
        job = db.get(SemanticIndexJob, source_id)
        if job is not None:
            job.status = "failed"
            job.last_error = type(exc).__name__
            db.commit()
        raise
    return True


def semantic_hits(db: Session, owner_id: str, query: str, domains: set[str] | None,
                  project_id: str | None, for_agent: bool, limit: int = 60,
                  allowed_source_ids=None, query_vector=None, min_similarity=None,
                  distinct_sources: bool = True):
    if not settings.semantic_enabled:
        return []
    import numpy as np
    stmt = (select(EvidenceEmbedding)
            .join(Source, Source.id == EvidenceEmbedding.source_id)
            .join(EvidenceChunk, EvidenceChunk.id == EvidenceEmbedding.evidence_id)
            .join(SourceRevision, SourceRevision.id == EvidenceEmbedding.revision_id)
            .where(EvidenceEmbedding.owner_id == owner_id,
                   EvidenceEmbedding.model == MODEL_NAME,
                   Source.owner_id == owner_id,
                   EvidenceChunk.revision_id == EvidenceEmbedding.revision_id,
                   SourceRevision.source_id == Source.id,
                   Source.deleted_at.is_(None),
                   Source.current_revision_id == EvidenceEmbedding.revision_id))
    if allowed_source_ids is not None:
        stmt = stmt.where(Source.id.in_(allowed_source_ids))
    if domains is not None:
        stmt = stmt.where(Source.domain_id.in_(domains))
    if project_id:
        from .models import SourceProject
        stmt = stmt.join(SourceProject, SourceProject.source_id == Source.id).where(
            SourceProject.project_id == project_id)
    if for_agent:
        stmt = stmt.where(Source.visibility != "app_only")
    rows = db.scalars(stmt).all()
    if not rows:
        return []
    global _search_model_unavailable
    try:
        query_vector = (query_vector() if callable(query_vector) else query_vector) if query_vector is not None else embed_query(query)
        _search_model_unavailable = False
    except Exception:
        _search_model_unavailable = True
        logger.exception("语义查询模型不可用；本次仅返回关键词结果")
        return []
    scored = []
    for embedding in rows:
        vector = np.frombuffer(embedding.vector, dtype=np.float32)
        if vector.shape != query_vector.shape:
            continue
        similarity = float(np.dot(query_vector, vector))
        threshold = settings.semantic_min_similarity if min_similarity is None else min_similarity
        if similarity >= threshold:
            scored.append((similarity, embedding))
    scored.sort(key=lambda row: row[0], reverse=True)
    if not distinct_sources:
        # Preserve the top source pool used by fusion; repeated windows from one
        # long article must not evict other articles before source ranking.
        source_ids = list(dict.fromkeys(embedding.source_id for _, embedding in scored))[:limit]
        counts = dict.fromkeys(source_ids, 0)
        result = []
        for similarity, embedding in scored:
            if embedding.source_id not in counts or counts[embedding.source_id] >= 20:
                continue
            counts[embedding.source_id] += 1
            source = db.get(Source, embedding.source_id)
            evidence = db.get(EvidenceChunk, embedding.evidence_id)
            revision = db.get(SourceRevision, embedding.revision_id)
            if source is not None and evidence is not None and revision is not None:
                result.append((similarity, source, evidence, revision, embedding))
        return result
    best_by_source = {}
    for similarity, embedding in scored:
        best_by_source.setdefault(embedding.source_id, (similarity, embedding))
        if len(best_by_source) >= limit:
            break
    result = []
    for source_id, (similarity, embedding) in best_by_source.items():
        source = db.get(Source, source_id)
        evidence = db.get(EvidenceChunk, embedding.evidence_id)
        revision = db.get(SourceRevision, embedding.revision_id)
        if source is not None and evidence is not None and revision is not None:
            result.append((similarity, source, evidence, revision, embedding))
    return result


def semantic_index_status(db: Session, owner_id: str) -> str:
    if not settings.semantic_enabled:
        return "disabled"
    if _search_model_unavailable:
        return "failed"
    statuses = set(db.scalars(
        select(SemanticIndexJob.status).join(Source, Source.id == SemanticIndexJob.source_id)
        .where(Source.owner_id == owner_id, Source.deleted_at.is_(None))
    ).all())
    if "failed" in statuses:
        return "failed"
    if statuses & {"pending", "running"}:
        return "pending"
    return "ready"
