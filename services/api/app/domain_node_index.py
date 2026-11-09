"""Rebuildable node embeddings; inference happens only in local index jobs."""
import hashlib
import json

from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.orm import aliased

from . import semantic
from .domain_contract import DomainSummaryRequest, DomainSummaryResponse, validate_domain_summary
from .domain_runtime import locked_state
from .models import DomainNodeEmbedding, DomainNodeIndexJob, DomainSummaryArtifact, DomainTopic


def published_nodes():
    from .domain_processing_nodes import logical_node_id
    artifact = DomainSummaryArtifact
    newer = aliased(DomainSummaryArtifact)
    topics = select(DomainTopic.id).where(DomainTopic.owner_id == artifact.owner_id,
        DomainTopic.domain_id == artifact.domain_id, DomainTopic.scope == artifact.scope)
    latest = ~exists(select(newer.id).where(newer.owner_id == artifact.owner_id,
        newer.domain_id == artifact.domain_id, newer.scope == artifact.scope, logical_node_id(newer) == logical_node_id(artifact),
        or_(newer.created_at > artifact.created_at,
            (newer.created_at == artifact.created_at) & (newer.id > artifact.id))))
    return select(artifact).where(or_(artifact.node_id == artifact.domain_id, logical_node_id(artifact).in_(topics)), latest)


def node_text(artifact):
    frozen = json.loads(artifact.snapshot_json)
    request = DomainSummaryRequest.model_validate(frozen['request'])
    response = DomainSummaryResponse.model_validate_json(artifact.response_json)
    if artifact.id != request.request_id or response.request_id != request.request_id or artifact.node_id != request.node_id:
        raise ValueError('Node artifact binding mismatch')
    validate_domain_summary(request, response.result)
    text = '\n'.join(point.text for point in
        [response.result.overview, *response.result.insights, *response.result.limitations])
    digest = hashlib.sha256((request.title + '\n' + text).encode()).hexdigest()
    return request.title, text, digest


def complete_vectors(db, artifact, text, digest):
    import numpy as np
    rows = db.scalars(select(DomainNodeEmbedding).where(DomainNodeEmbedding.artifact_id == artifact.id)
        .order_by(DomainNodeEmbedding.ordinal)).all()
    expected = [(start, end) for start, end, _ in semantic.windows(text)]
    if [(row.start, row.end) for row in rows] != expected or [row.ordinal for row in rows] != list(range(len(expected))):
        return False
    sizes = set()
    for row in rows:
        if row.model != semantic.MODEL_NAME or row.text_sha256 != digest:
            return False
        try:
            vector = np.frombuffer(row.vector, dtype=np.float32)
        except ValueError:
            return False
        if not vector.size or not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1, atol=1e-4):
            return False
        sizes.add(vector.size)
    return len(sizes) == 1


def ensure_node_index(db, *, owner_id=None):
    if not semantic.settings.semantic_enabled:
        return
    nodes = published_nodes()
    if owner_id is not None:
        nodes = nodes.where(DomainSummaryArtifact.owner_id == owner_id)
    current = db.scalars(nodes.order_by(DomainSummaryArtifact.id)).all()
    ids = [artifact.id for artifact in current]
    vectors = delete(DomainNodeEmbedding).where(DomainNodeEmbedding.artifact_id.not_in(ids))
    jobs = delete(DomainNodeIndexJob).where(DomainNodeIndexJob.artifact_id.not_in(ids))
    if owner_id is not None:
        owned = select(DomainSummaryArtifact.id).where(DomainSummaryArtifact.owner_id == owner_id)
        vectors = vectors.where(DomainNodeEmbedding.artifact_id.in_(owned))
        jobs = jobs.where(DomainNodeIndexJob.artifact_id.in_(owned))
    db.execute(vectors)
    db.execute(jobs)
    for artifact in current:
        try:
            _, text, digest = node_text(artifact)
        except (ValueError, TypeError, KeyError):
            continue
        job = db.get(DomainNodeIndexJob, artifact.id)
        if job is None:
            db.add(DomainNodeIndexJob(artifact_id=artifact.id, model=semantic.MODEL_NAME, text_sha256=digest))
        elif job.model != semantic.MODEL_NAME or job.text_sha256 != digest:
            db.execute(delete(DomainNodeEmbedding).where(DomainNodeEmbedding.artifact_id == artifact.id))
            job.model, job.text_sha256, job.status, job.last_error = semantic.MODEL_NAME, digest, 'pending', None
        elif job.status == 'completed' and not complete_vectors(db, artifact, text, digest):
            job.status, job.last_error = 'pending', None
    db.commit()


def interrupt_node_index(db):
    # Local inference is safe to rebuild. This never resumes a paid AI job.
    db.execute(update(DomainNodeIndexJob).where(DomainNodeIndexJob.status == 'running').values(status='pending'))


def index_node_once(db):
    if not semantic.settings.semantic_enabled:
        return False
    import numpy as np
    job = db.scalar(select(DomainNodeIndexJob).where(DomainNodeIndexJob.status == 'pending')
        .order_by(DomainNodeIndexJob.artifact_id).limit(1))
    if job is None:
        return False
    identifier = job.artifact_id
    claimed = db.execute(update(DomainNodeIndexJob).where(DomainNodeIndexJob.artifact_id == identifier,
        DomainNodeIndexJob.status == 'pending').values(status='running', attempts=DomainNodeIndexJob.attempts + 1)).rowcount
    db.commit()
    if not claimed:
        return True
    try:
        artifact = db.get(DomainSummaryArtifact, identifier)
        if artifact is None:
            return True
        title, text, digest = node_text(artifact)
        parts = list(semantic.windows(text))
        owner, domain, scope = artifact.owner_id, artifact.domain_id, artifact.scope
        # End the read transaction as well: no SQLite writer lock during inference.
        db.commit()
        vectors = semantic.embed_documents([title + ' ' + part for _, _, part in parts])
        if len(vectors) != len(parts):
            raise ValueError('Incomplete node embeddings')
        for raw in vectors:
            vector = np.frombuffer(raw, dtype=np.float32)
            if not vector.size or not np.isfinite(vector).all() or not np.isclose(np.linalg.norm(vector), 1, atol=1e-4):
                raise ValueError('Invalid node embedding')
        if len({len(raw) for raw in vectors}) != 1:
            raise ValueError('Inconsistent node embedding dimensions')
        locked_state(db, owner, domain, scope)
        job = db.get(DomainNodeIndexJob, identifier)
        artifact = db.scalar(published_nodes().where(DomainSummaryArtifact.id == identifier))
        if job is None or job.status != 'running' or artifact is None:
            db.rollback()
            return True
        if node_text(artifact)[2] != digest or job.text_sha256 != digest or job.model != semantic.MODEL_NAME:
            raise ValueError('Node index inputs changed')
        db.execute(delete(DomainNodeEmbedding).where(DomainNodeEmbedding.artifact_id == identifier))
        for ordinal, ((start, end, _), vector) in enumerate(zip(parts, vectors, strict=True)):
            db.add(DomainNodeEmbedding(artifact_id=identifier, ordinal=ordinal, model=semantic.MODEL_NAME,
                text_sha256=digest, start=start, end=end, vector=vector))
        job.status, job.last_error = 'completed', None
        db.commit()
    except Exception as error:
        db.rollback()
        job = db.get(DomainNodeIndexJob, identifier)
        if job and job.status == 'running':
            job.status, job.last_error = 'failed', type(error).__name__
            db.commit()
    return True


def node_index_status(db, artifact_id):
    if not semantic.settings.semantic_enabled:
        return 'disabled'
    job = db.get(DomainNodeIndexJob, artifact_id)
    if job and job.status == 'completed':
        artifact = db.get(DomainSummaryArtifact, artifact_id)
        try:
            _, text, digest = node_text(artifact)
            return 'ready' if complete_vectors(db, artifact, text, digest) else 'pending'
        except (ValueError, TypeError, KeyError, AttributeError):
            return 'failed'
    return job.status if job else 'pending'
