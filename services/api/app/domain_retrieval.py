"""Current, permission-contained node summaries help find original evidence.

This first retrieval channel uses bounded published domain/topic text. It does
not embed on query, generate new summaries or replace original-text channels.
"""
import json


from sqlalchemy import case, exists, func, or_, select

from .domain_contract import DomainSummaryRequest, DomainSummaryResponse
from .domain_summary_jobs import current_node_result
from .domain_runtime import locked_state
from .domain_processing import DomainInputManifest
from .domain_query_snapshot import NodeQuerySnapshot, input_batches
from .errors import ApiError
from .models import DomainTrash, DomainSummaryArtifact, DomainTopic, EvidenceChunk, Source, SourceRevision


def eligible_nodes(owner, scope, for_agent):
    """Apply every dependency's visibility in SQL before loading model output."""
    from .domain_processing_nodes import logical_node_id
    artifact = DomainSummaryArtifact
    node_scope = 'shareable' if for_agent else 'app'
    safe_snapshot = case((func.json_valid(artifact.snapshot_json) == 1, artifact.snapshot_json), else_='{}')
    dependencies = func.coalesce(func.json_extract(safe_snapshot, '$.global_manifest.sources'),
                                 func.json_extract(safe_snapshot, '$.manifest.sources'), '[]')
    def contained(path, name):
        entries = func.json_each(safe_snapshot, path).table_valued('key', 'value').alias(name)
        identifier = func.json_extract(entries.c.value, '$.source_id')
        return ~exists(select(1).select_from(entries).where(
            or_(identifier.is_(None), identifier.not_in(scope.with_only_columns(Source.id)))))
    # Virtual processing batches are not public topic navigation nodes yet.
    topics = select(DomainTopic.id).where(DomainTopic.owner_id == owner, DomainTopic.scope == node_scope)
    # Rank metadata before dependency visibility: an inaccessible newer node
    # must suppress the older narrower version, rather than revive old output.
    # Window ranking avoids reparsing each newer JSON snapshot for every node.
    latest_rows = select(artifact.id.label('id'), func.row_number().over(
        partition_by=(artifact.owner_id, artifact.domain_id, artifact.scope, logical_node_id(artifact)),
        order_by=(artifact.created_at.desc(), artifact.id.desc())).label('node_rank')).where(
        artifact.owner_id == owner, artifact.scope == node_scope,
        artifact.domain_id.in_(scope.with_only_columns(Source.domain_id).distinct())).subquery()
    latest = artifact.id.in_(select(latest_rows.c.id).where(latest_rows.c.node_rank == 1))
    return select(artifact).where(~artifact.domain_id.in_(select(DomainTrash.domain_id)), artifact.owner_id == owner, artifact.scope == node_scope,
        artifact.domain_id.in_(scope.with_only_columns(Source.domain_id).distinct()),
        or_(artifact.node_id == artifact.domain_id, logical_node_id(artifact).in_(topics)),
        func.json_array_length(dependencies) > 0,
        contained('$.manifest.sources', 'node_dependencies'),
        contained('$.global_manifest.sources', 'global_dependencies'),
        contained('$.request.evidence', 'node_originals'),
        contained('$.topic_manifest.sources', 'topic_dependencies'),
        contained('$.processing_manifest.sources', 'processing_dependencies'), latest).order_by(artifact.domain_id, artifact.node_id)


def resolve_original(db, owner, entry, scope, validation_context=None):
    source = (validation_context.source(owner, entry.source_id, scope) if validation_context
              else db.scalar(scope.where(Source.id == entry.source_id)))
    evidence = db.get(EvidenceChunk, entry.original_evidence_id or entry.id)
    revision = db.get(SourceRevision, entry.revision_id)
    if (source is None or source.owner_id != owner or source.deleted_at is not None
            or source.processing_status in ('deleting', 'deleted')
            or source.current_revision_id != entry.revision_id or revision is None
            or revision.source_id != source.id or evidence is None or evidence.revision_id != revision.id):
        return None
    start = entry.text_start or 0
    if start < 0 or evidence.text[start:start + len(entry.text)] != entry.text:
        return None
    if entry.text_end is not None and entry.text_end != start + len(entry.text):
        return None
    return source, evidence, revision, start


def node_hits(db, owner, query, tokens, scope, for_agent=False, limit=60, query_vector=None):
    # locked_state expires ORM rows. Never erase an unflushed caller mutation,
    # especially on sessions with autoflush disabled.
    if db.new or db.dirty or db.deleted:
        return {}
    if db.get_bind().dialect.name != 'sqlite' or not tokens:
        return {}
    from . import semantic
    from .domain_node_index import node_text
    from .models import DomainNodeEmbedding, DomainNodeIndexJob
    # Fetch only gated metadata before inference. Do not repeat the correlated
    # containment query inside the cache-existence query and domain lookup.
    metadata = list(db.execute(eligible_nodes(owner, scope, for_agent)
        .with_only_columns(DomainSummaryArtifact.id, DomainSummaryArtifact.domain_id)))
    if not metadata:
        return {}
    vector = None
    if semantic.settings.semantic_enabled and query_vector is not None:
        import numpy as np
        cached = any(db.scalar(select(DomainNodeEmbedding.artifact_id).where(
            DomainNodeEmbedding.artifact_id.in_(batch), DomainNodeEmbedding.model == semantic.MODEL_NAME).limit(1))
            for batch in input_batches([row.id for row in metadata]))
        if cached:
            try:
                vector = query_vector()
                semantic._search_model_unavailable = False
            except Exception:
                semantic._search_model_unavailable = True
    # Reapply the full gate after acquiring the SQLite write lock. A newly
    # published wider node cannot slip into the generated-output read.
    domain = metadata[0].domain_id
    try:
        locked_state(db, owner, domain, 'shareable' if for_agent else 'app')
    except ApiError:
        return {}
    artifacts = list(db.scalars(eligible_nodes(owner, scope, for_agent)))
    if not artifacts:
        return {}
    jobs, embeddings = {}, {}
    if vector is not None:
        artifact_ids = [artifact.id for artifact in artifacts]
        for batch in input_batches(artifact_ids):
            for job in db.scalars(select(DomainNodeIndexJob).where(
                    DomainNodeIndexJob.artifact_id.in_(batch), DomainNodeIndexJob.model == semantic.MODEL_NAME,
                    DomainNodeIndexJob.status == 'completed')):
                jobs[job.artifact_id] = job
            for embedding in db.scalars(select(DomainNodeEmbedding).where(
                    DomainNodeEmbedding.artifact_id.in_(batch), DomainNodeEmbedding.model == semantic.MODEL_NAME)):
                embeddings.setdefault(embedding.artifact_id, []).append(embedding)
    candidates = []
    for artifact in artifacts:
        try:
            frozen = json.loads(artifact.snapshot_json)
            response = DomainSummaryResponse.model_validate_json(artifact.response_json)
            if response.request_id != artifact.id:
                raise ValueError('Node artifact and request identity mismatch')
            points = [response.result.overview, *response.result.insights, *response.result.limitations]
            text = '\n'.join(point.text for point in points)
            haystack = text.casefold()
            score = sum(token in haystack for token in tokens) + (5 if query.casefold() in haystack else 0)
            similarity = None
            if vector is not None:
                digest = node_text(artifact)[2]
                job = jobs.get(artifact.id)
                if job and job.text_sha256 == digest:
                    for embedding in embeddings.get(artifact.id, []):
                        if embedding.text_sha256 != digest:
                            continue
                        if embedding.start < 0 or embedding.end <= embedding.start or embedding.end > len(text):
                            continue
                        try:
                            stored = np.frombuffer(embedding.vector, dtype=np.float32)
                        except ValueError:
                            continue
                        if stored.shape != vector.shape or not np.isfinite(stored).all() or not np.isclose(np.linalg.norm(stored), 1, atol=1e-4):
                            continue
                        value = float(np.dot(vector, stored))
                        if value >= semantic.settings.semantic_min_similarity:
                            similarity = max(similarity or value, value)
            if not score and similarity is None:
                continue
            DomainSummaryRequest.model_validate(frozen['request'])
            for name in ('manifest', 'global_manifest', 'topic_manifest', 'processing_manifest'):
                if name in frozen:
                    DomainInputManifest.model_validate(frozen[name])
            candidates.append((artifact, frozen, response, points, text, score, similarity))
        except (ApiError, ValueError, TypeError, KeyError, AttributeError):
            continue
    if not candidates:
        return {}
    try:
        validation_context = NodeQuerySnapshot(db, owner, scope, [row[0] for row in candidates])
    except (ApiError, ValueError, TypeError, KeyError):
        return {}
    ranked = []
    for artifact, frozen, response, points, text, score, similarity in candidates:
        try:
            validation_context.check()
            topic_id = frozen.get('published_topic_id') or (artifact.node_id if artifact.node_id != artifact.domain_id else None)
            current = current_node_result(db, owner, artifact.domain_id, artifact.scope, topic_id,
                                          validation_context=validation_context, artifact=artifact)
            if not current['available'] or current['id'] != artifact.id:
                continue
            request = DomainSummaryRequest.model_validate(frozen['request'])
            evidence = {entry.id: entry for entry in request.evidence}
            # Use matching statements' references; never present the model prose
            # itself as original evidence or expand to an arbitrary first chunk.
            references = list(dict.fromkeys(citation for point in points
                if similarity is not None or any(token in point.text.casefold() for token in tokens)
                for citation in point.citations))
            context = dict(artifact_id=artifact.id, node_id=topic_id or artifact.node_id, domain_id=artifact.domain_id,
                kind='topic' if topic_id else 'domain', title=request.title, snippet=text[:800],
                coverage=current['coverage'], role='retrieval_hint',
                match_type='hybrid' if score and similarity is not None else 'semantic' if similarity is not None else 'keyword')
            for citation in references:
                resolved = resolve_original(db, owner, evidence[citation], scope, validation_context=validation_context)
                if resolved is not None:
                    ranked.append((score + (similarity or 0), artifact.id, resolved, context))
        except (ApiError, ValueError, TypeError, KeyError, AttributeError):
            # Missing/stale/corrupt nodes must never break original retrieval.
            continue
    try:
        validation_context.check()
    except ApiError:
        return {}
    ranked.sort(key=lambda row: (-row[0], row[1], row[2][0].id))
    hits = {}
    for _, _, original, context in ranked:
        hits.setdefault(original[0].id, (*original, context))
        if len(hits) >= limit:
            break
    return hits
