"""Scoped summary and tag retrieval; all returned evidence remains original text."""
from __future__ import annotations

import json

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .errors import ApiError
from .knowledge_tags import normalize
from .models import AIArtifact, EvidenceChunk, KnowledgeTag, KnowledgeTagAlias, Source, SourceRevision, TagAssignment


def valid_assignments():
    return or_(TagAssignment.origin == "manual",
               (TagAssignment.origin == "ai") & (TagAssignment.revision_id == Source.current_revision_id))


def visible_tag_ids(base_scope):
    return select(TagAssignment.tag_id).join(Source, Source.id == TagAssignment.source_id).where(
        Source.id.in_(base_scope.with_only_columns(Source.id)), valid_assignments())


def filter_tags(db: Session, owner: str, request, scope, for_agent: bool):
    """Resolve exact names and aliases before limiting any retrieval channel."""
    catalog = select(KnowledgeTag).where(KnowledgeTag.owner_id == owner)
    if for_agent:
        catalog = catalog.where(KnowledgeTag.id.in_(visible_tag_ids(scope)))
    requested = set(getattr(request, "tag_ids", None) or [])
    if requested:
        known = set(db.scalars(catalog.with_only_columns(KnowledgeTag.id).where(KnowledgeTag.id.in_(requested))))
        if known != requested:
            raise ApiError(404, "not_found", "标签不存在")
    query = getattr(request, "tag_query", None)
    if query:
        key = normalize(query)[1]
        names = set(db.scalars(catalog.with_only_columns(KnowledgeTag.id).where(
            or_(KnowledgeTag.normalized_name == key, KnowledgeTag.id.in_(select(KnowledgeTagAlias.tag_id)
                .where(KnowledgeTagAlias.owner_id == owner, KnowledgeTagAlias.normalized_name == key))))))
        requested |= names
    if getattr(request, "tag_ids", None) or query:
        assignments = select(TagAssignment.source_id).join(Source, Source.id == TagAssignment.source_id).where(
            Source.id.in_(scope.with_only_columns(Source.id)), TagAssignment.tag_id.in_(requested), valid_assignments())
        scope = scope.where(Source.id.in_(assignments))
    return scope


def tag_hits(db: Session, owner: str, query: str, tokens: list[str], scope, limit: int = 60) -> dict:
    values = list(dict.fromkeys([query.casefold().strip(), *tokens[:64]]))
    values = [value for value in values if value]
    if not values:
        return {}
    # Scan the vocabulary only, not every source body. SQL applies source scope first.
    tag_stmt = select(KnowledgeTag).where(KnowledgeTag.owner_id == owner,
        KnowledgeTag.id.in_(visible_tag_ids(scope)))
    aliases = select(KnowledgeTagAlias.tag_id).where(KnowledgeTagAlias.owner_id == owner,
        or_(*(KnowledgeTagAlias.normalized_name.contains(value, autoescape=True) for value in values)))
    tag_stmt = tag_stmt.where(or_(KnowledgeTag.id.in_(aliases),
        *(KnowledgeTag.normalized_name.contains(value, autoescape=True) for value in values)))
    found = db.scalars(tag_stmt.order_by(KnowledgeTag.id)).all()
    if not found:
        return {}
    source_ids = db.scalars(select(TagAssignment.source_id).join(Source, Source.id == TagAssignment.source_id)
        .where(Source.id.in_(scope.with_only_columns(Source.id)), TagAssignment.tag_id.in_([t.id for t in found]), valid_assignments())
        .distinct().order_by(TagAssignment.source_id)).all()
    result = {}
    for source_id in source_ids:
        source = db.get(Source, source_id)
        revision = db.get(SourceRevision, source.current_revision_id) if source and source.current_revision_id else None
        evidence = db.scalar(select(EvidenceChunk).where(EvidenceChunk.revision_id == revision.id)
            .order_by(EvidenceChunk.ordinal, EvidenceChunk.id).limit(1)) if revision else None
        if source and revision and evidence:
            result[source.id] = (source, evidence, revision)
            if len(result) == limit:
                break
    return result


def source_tags(db: Session, source: Source) -> list[dict]:
    return source_tags_many(db, source.owner_id, [source.id]).get(source.id, [])


def source_tags_many(db: Session, owner: str, source_ids) -> dict[str, list[dict]]:
    """Same live assignment rules/order for one source or a scoped input batch."""
    result = {}
    for source_id, tag, assignment in db.execute(select(TagAssignment.source_id, KnowledgeTag, TagAssignment)
            .join(KnowledgeTag, TagAssignment.tag_id == KnowledgeTag.id)
            .join(Source, Source.id == TagAssignment.source_id)
            .where(Source.id.in_(source_ids), KnowledgeTag.owner_id == owner, valid_assignments())
            .order_by(KnowledgeTag.name, TagAssignment.origin)):
        catalog = result.setdefault(source_id, {})
        item = catalog.setdefault(tag.id, {"id": tag.id, "name": tag.name, "dimension": tag.dimension, "origins": []})
        item["origins"].append(assignment.origin)
    return {source_id: list(tags.values()) for source_id, tags in result.items()}


def summary_evidence(db: Session, source: Source, artifact: AIArtifact):
    """Validate every summary reference and resolve virtual slices to original evidence."""
    try:
        snapshot, result = json.loads(artifact.snapshot_json), json.loads(artifact.result_json)
        entries = {item["id"]: item for item in snapshot["evidence"]}
        groups = [result["summary_citations"], *[i["citations"] for i in result.get("insights", [])],
                  *[i["citations"] for i in result.get("limitations", [])]]
        if result.get("overview"):
            groups.append(result["overview"]["citations"])
        references = list(dict.fromkeys(identifier for group in groups for identifier in group))
        if not references or artifact.source_id != source.id or artifact.owner_id != source.owner_id or artifact.revision_id != source.current_revision_id:
            return None
        resolved = []
        for identifier in references:
            item = entries.get(identifier)
            if item is None:
                return None
            original_id = item.get("original_evidence_id") or identifier
            evidence = db.get(EvidenceChunk, original_id)
            if evidence is None or evidence.revision_id != source.current_revision_id:
                return None
            revision = db.get(SourceRevision, evidence.revision_id)
            if revision is None or revision.source_id != source.id:
                return None
            start = item.get("text_start", 0)
            end = item.get("text_end")
            if not isinstance(start, int) or isinstance(start, bool) or start < 0:
                return None
            if end is not None:
                if not isinstance(end, int) or isinstance(end, bool) or end - start != len(item["text"]) or end > len(evidence.text):
                    return None
            elif start:
                return None
            if item["text"] != evidence.text[start:end] and not (end is None and item["text"] == evidence.text[:len(item["text"])]):
                return None
            resolved.append((evidence, revision, start))
        return resolved[0]
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def summary_hits(db: Session, owner: str, query: str, tokens: list[str], scope, query_vector, limit=60):
    from . import semantic
    from .summary_index import current_summary_artifact, summary_text
    from .models import SummaryEmbedding, SummarySearchTerm
    index_tokens = [token for token in tokens[:64] if len(token) <= 128]
    keyword_rows = db.scalars(select(AIArtifact).join(Source, Source.id == AIArtifact.source_id)
        .where(Source.id.in_(scope.with_only_columns(Source.id)), AIArtifact.owner_id == owner,
            AIArtifact.id.in_(select(SummarySearchTerm.artifact_id).where(SummarySearchTerm.owner_id == owner,
                SummarySearchTerm.term.in_(index_tokens))))).all() if index_tokens else []
    vector_rows = db.scalars(select(SummaryEmbedding).join(Source, Source.id == SummaryEmbedding.source_id)
        .join(AIArtifact, AIArtifact.id == SummaryEmbedding.artifact_id)
        .where(Source.id.in_(scope.with_only_columns(Source.id)), SummaryEmbedding.owner_id == owner,
            SummaryEmbedding.revision_id == Source.current_revision_id, SummaryEmbedding.model == semantic.MODEL_NAME,
            AIArtifact.source_id == Source.id, AIArtifact.owner_id == owner, AIArtifact.revision_id == SummaryEmbedding.revision_id)).all() if semantic.settings.semantic_enabled else []
    vector = None
    if vector_rows:
        import numpy as np
        try:
            vector = query_vector()
            semantic._search_model_unavailable = False
        except Exception:
            semantic._search_model_unavailable = True
    artifacts = {row.id: row for row in keyword_rows}
    for row in vector_rows:
        artifact = db.get(AIArtifact, row.artifact_id)
        if artifact:
            artifacts.setdefault(artifact.id, artifact)
    valid = {}
    latest_by_source = {}
    for artifact in artifacts.values():
        source = db.get(Source, artifact.source_id)
        if source is None:
            continue
        if source.id not in latest_by_source:
            latest_by_source[source.id] = current_summary_artifact(db, source)
        current = latest_by_source[source.id]
        if current is None or current.id != artifact.id:
            continue
        citation = summary_evidence(db, source, artifact)
        if citation:
            valid[artifact.id] = (source, artifact, citation, summary_text(artifact))
    keyword_scores = []
    for artifact in keyword_rows:
        if artifact.id in valid:
            text = valid[artifact.id][3].casefold()
            score = sum(token in text for token in tokens) + (5 if query.casefold() in text else 0)
            if score:
                keyword_scores.append((score, artifact.id))
    keyword_scores.sort(key=lambda item: (-item[0], item[1]))
    vector_scores = []
    if vector is not None:
        for embedding in vector_rows:
            if embedding.artifact_id not in valid:
                continue
            source, artifact, citation, text = valid[embedding.artifact_id]
            if embedding.start < 0 or embedding.end <= embedding.start or embedding.end > len(text):
                continue
            try:
                stored = np.frombuffer(embedding.vector, dtype=np.float32)
            except ValueError:
                continue
            if stored.shape != vector.shape:
                continue
            score = float(np.dot(vector, stored))
            if score >= semantic.settings.semantic_min_similarity:
                vector_scores.append((score, embedding.artifact_id, embedding.start))
    vector_scores.sort(key=lambda item: (-item[0], item[1]))
    lexical, vectors = {}, {}
    for score, artifact_id in keyword_scores:
        source, artifact, citation, text = valid[artifact_id]
        lexical.setdefault(source.id, (source, artifact, citation, text, 0))
        if len(lexical) >= limit:
            break
    for score, artifact_id, start in vector_scores:
        source, artifact, citation, text = valid[artifact_id]
        vectors.setdefault(source.id, (source, artifact, citation, text, start))
        if len(vectors) >= limit:
            break
    return lexical, vectors
