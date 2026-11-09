"""Owner-only domain -> existing topic -> original-source navigation."""
from fastapi import Depends
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import AppPrincipal, require_app_principal
from .database import get_db
from .domain_builds import router
from .domain_node_index import ensure_node_index, node_index_status, published_nodes
from .domain_runtime import locked_state
from .domain_summary_jobs import NodeSelection, current_node_result
from .domain_topics import require_owned_domain, require_topic, source_scope
from .errors import ApiError
from .models import DomainNodeIndexJob, DomainSummaryArtifact, DomainTopic, DomainTopicMember, Source


class NavigationRequest(NodeSelection):
    topic_offset: int = Field(default=0, ge=0)
    source_offset: int = Field(default=0, ge=0)
    limit: int = Field(default=40, ge=1, le=100)


def navigate(db, owner, domain, body):
    locked_state(db, owner, domain, body.scope)
    domain_row = require_owned_domain(db, owner, domain)
    topic = require_topic(db, owner, domain, body.topic_id) if body.topic_id else None
    if topic and topic.scope != body.scope:
        raise ApiError(404, 'not_found', '主题不存在')
    sources = source_scope(owner, domain, body.scope)
    visible = sources.with_only_columns(Source.id)
    counts = select(DomainTopicMember.topic_id, func.count().label('source_count')).where(
        DomainTopicMember.included.is_(True), DomainTopicMember.source_id.in_(visible))\
        .group_by(DomainTopicMember.topic_id).subquery()
    children = select(DomainTopic, func.coalesce(counts.c.source_count, 0)).outerjoin(
        counts, counts.c.topic_id == DomainTopic.id).where(DomainTopic.owner_id == owner,
        DomainTopic.domain_id == domain, DomainTopic.scope == body.scope,
        DomainTopic.parent_id == (topic.id if topic else None)).order_by(DomainTopic.id)
    if body.scope == 'shareable':
        # Keep an empty parent only if it has a visible populated child.
        child = select(DomainTopic.parent_id).where(DomainTopic.owner_id == owner,
            DomainTopic.domain_id == domain, DomainTopic.scope == body.scope,
            DomainTopic.id.in_(select(DomainTopicMember.topic_id).where(
                DomainTopicMember.included.is_(True), DomainTopicMember.source_id.in_(visible))))
        children = children.where((counts.c.source_count > 0) | DomainTopic.id.in_(child))
    rows = db.execute(children.offset(body.topic_offset).limit(body.limit + 1)).all()
    if topic:
        sources = sources.where(Source.id.in_(select(DomainTopicMember.source_id).where(
            DomainTopicMember.topic_id == topic.id, DomainTopicMember.included.is_(True))))
    members = db.scalars(sources.order_by(Source.id).offset(body.source_offset).limit(body.limit + 1)).all()
    try:
        overview = current_node_result(db, owner, domain, body.scope, body.topic_id)
    except (ValueError, TypeError, KeyError):
        overview = dict(available=False, reason='invalid_node_data')
    return dict(domain_id=domain, scope=body.scope,
        node=dict(id=topic.id if topic else domain, kind='topic' if topic else 'domain',
                  name=topic.name if topic else domain_row.name, parent_id=topic.parent_id if topic else None),
        overview=overview,
        index_status=node_index_status(db, overview['id']) if overview['available'] else 'unavailable',
        topics=dict(items=[dict(id=t.id, name=t.name, parent_id=t.parent_id, source_count=count) for t, count in rows[:body.limit]],
            next_offset=body.topic_offset + body.limit if len(rows) > body.limit else None),
        sources=dict(items=[dict(id=s.id, title=s.title, revision_id=s.current_revision_id) for s in members[:body.limit]],
            next_offset=body.source_offset + body.limit if len(members) > body.limit else None))


@router.post('/{domain_id}/analysis/navigation')
def read_navigation(domain_id: str, body: NavigationRequest,
                    principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    result = navigate(db, principal.owner_id, domain_id, body)
    db.commit()
    return result


@router.post('/{domain_id}/analysis/index/rebuild')
def rebuild_node_index(domain_id: str, body: NodeSelection,
                       principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    from . import semantic
    require_owned_domain(db, principal.owner_id, domain_id)
    if body.topic_id:
        topic = require_topic(db, principal.owner_id, domain_id, body.topic_id)
        if topic.scope != body.scope:
            raise ApiError(404, 'not_found', '主题不存在')
    if not semantic.settings.semantic_enabled:
        return dict(queued=0, status='disabled')
    ensure_node_index(db, owner_id=principal.owner_id)
    nodes = published_nodes().where(DomainSummaryArtifact.owner_id == principal.owner_id,
        DomainSummaryArtifact.domain_id == domain_id, DomainSummaryArtifact.scope == body.scope)
    if body.topic_id:
        from .domain_processing_nodes import logical_node_id
        nodes = nodes.where(logical_node_id(DomainSummaryArtifact) == body.topic_id)
    count = 0
    for artifact in db.scalars(nodes):
        job = db.get(DomainNodeIndexJob, artifact.id)
        if job and job.status != 'running':
            job.status, job.last_error = 'pending', None
            count += 1
    db.commit()
    return dict(queued=count, status='pending' if count else 'not_built')


@router.post('/{domain_id}/analysis/processing-nodes/{artifact_id}')
def read_processing_node(domain_id: str, artifact_id: str, body: NodeSelection,
                         principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    from .domain_processing_nodes import processing_view
    if body.topic_id is not None:
        raise ApiError(422, 'artifact_selection_required', '请按加工产物ID下钻')
    result = processing_view(db, principal.owner_id, domain_id, body.scope, artifact_id)
    db.commit()
    return result
