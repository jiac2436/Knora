"""Local candidate topics and owner navigation, without model inference."""
from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from pydantic import Field, field_validator
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from .ai_contract import StrictModel
from .auth import AppPrincipal, require_app_principal
from .database import get_db
from .domain_processing import DomainScope
from .domain_runtime import locked_state
from .errors import ApiError
from .models import Domain, DomainTopic, DomainTopicMember, DomainProcessingState, DomainTopicIndexState, Source
from .summary_retrieval import source_tags

router = APIRouter(prefix="/v1/domains", tags=["domain-topics"])


def require_owned_domain(db: Session, owner: str, domain: str) -> Domain:
    row = db.scalar(select(Domain).where(Domain.id == domain, Domain.owner_id == owner))
    if row is None:
        raise ApiError(404, "not_found", "知识域不存在")
    return row


def source_scope(owner: str, domain: str, scope: DomainScope):
    if scope not in {"app", "shareable"}:
        raise ValueError("Unsupported domain scope")
    query = select(Source).where(Source.owner_id == owner, Source.domain_id == domain,
        Source.deleted_at.is_(None), Source.processing_status.not_in(("deleting", "deleted")))
    return query.where(Source.visibility == "app_and_agents") if scope == "shareable" else query


def require_topic(db: Session, owner: str, domain: str, topic_id: str) -> DomainTopic:
    require_owned_domain(db, owner, domain)
    topic = db.scalar(select(DomainTopic).where(DomainTopic.id == topic_id,
        DomainTopic.owner_id == owner, DomainTopic.domain_id == domain))
    if topic is None:
        raise ApiError(404, "not_found", "主题不存在")
    return topic


def rebuild_local_topics(db: Session, owner: str, domain: str, scope: DomainScope) -> dict:
    """Candidates from existing topic tags, not AI claims or a final taxonomy.

    Two distinct live sources are required per automatic group. Manual choices
    survive rebuilding, including explicit exclusion from automatic groups.
    Caller commits the short transaction.
    """
    state = locked_state(db, owner, domain, scope)
    sources = db.scalars(source_scope(owner, domain, scope).order_by(Source.id)).all()
    groups, names = defaultdict(set), {}
    for source in sources:
        for tag in source_tags(db, source):
            if tag["dimension"] == "topic":
                groups[tag["id"]].add(source.id)
                names[tag["id"]] = tag["name"]
    topics = db.scalars(select(DomainTopic).where(DomainTopic.owner_id == owner,
        DomainTopic.domain_id == domain, DomainTopic.scope == scope)).all()
    automatic = {topic.automatic_key: topic for topic in topics if topic.automatic_key}
    automatic_ids = [topic.id for topic in automatic.values()]
    # Keep manual additions/removals, including those for old automatic topics.
    db.execute(delete(DomainTopicMember).where(DomainTopicMember.topic_id.in_(automatic_ids),
                                              DomainTopicMember.manual.is_(False)))
    for tag_id, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        key = "tag:" + tag_id
        topic = automatic.get(key)
        if topic is None:
            topic = DomainTopic(owner_id=owner, domain_id=domain, scope=scope,
                                name=names[tag_id], automatic_key=key)
            db.add(topic); db.flush()
        elif not topic.name_fixed:
            topic.name = names[tag_id]
        for source_id in sorted(members):
            if db.get(DomainTopicMember, (topic.id, source_id)) is None:
                db.add(DomainTopicMember(topic_id=topic.id, source_id=source_id))
    db.flush()
    index = db.get(DomainTopicIndexState, (domain, scope))
    if index is None:
        db.add(DomainTopicIndexState(domain_id=domain, scope=scope, generation=state.generation))
    else:
        index.generation = state.generation
    db.flush()
    return {"source_count": len(sources), "method": "local_tag_candidates", "model_calls": 0}


def prepare_local_topics_once(db: Session) -> bool:
    """One local domain/scope per worker tick; independent of AI authorization."""
    state = db.scalar(select(DomainProcessingState).outerjoin(DomainTopicIndexState,
        (DomainTopicIndexState.domain_id == DomainProcessingState.domain_id) &
        (DomainTopicIndexState.scope == DomainProcessingState.scope)).where(or_(
            DomainTopicIndexState.domain_id.is_(None),
            DomainTopicIndexState.generation != DomainProcessingState.generation))
        .order_by(DomainProcessingState.domain_id, DomainProcessingState.scope).limit(1))
    if state is None:
        return False
    rebuild_local_topics(db, state.owner_id, state.domain_id, state.scope)
    db.commit()
    return True


def ensure_local_topics(db: Session) -> None:
    """Restore/startup creates only local work, never an AI authorization."""
    domains = list(db.execute(select(Domain.id, Domain.owner_id).order_by(Domain.id)))
    for domain_id, owner_id in domains:
        for scope in ('app', 'shareable'):
            locked_state(db, owner_id, domain_id, scope)
    db.commit()


def topic_directory(db: Session, owner: str, domain: str, scope: DomainScope,
                    offset: int = 0, limit: int = 40) -> dict:
    require_owned_domain(db, owner, domain)
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("Invalid pagination")
    available = set(db.scalars(source_scope(owner, domain, scope).with_only_columns(Source.id)))
    topics = db.scalars(select(DomainTopic).where(DomainTopic.owner_id == owner,
        DomainTopic.domain_id == domain, DomainTopic.scope == scope).order_by(DomainTopic.id)).all()
    members = defaultdict(set)
    for topic_id, source_id in db.execute(select(DomainTopicMember.topic_id, DomainTopicMember.source_id)
        .where(DomainTopicMember.topic_id.in_([topic.id for topic in topics]),
               DomainTopicMember.included.is_(True), DomainTopicMember.source_id.in_(available))):
        members[topic_id].add(source_id)
    active = [topic for topic in topics if members[topic.id] or
              (scope == "app" and (topic.automatic_key is None or topic.name_fixed))]
    # Keep ancestors visible when a populated child remains after regrouping.
    active_ids = {topic.id for topic in active}
    active_ids.update(topic.parent_id for topic in active if topic.parent_id)
    active = [topic for topic in topics if topic.id in active_ids]
    assigned = set().union(*members.values()) if members else set()
    page = active[offset:offset + limit]
    state = db.get(DomainProcessingState, (domain, scope))
    index = db.get(DomainTopicIndexState, (domain, scope))
    return dict(domain_id=domain, scope=scope, source_count=len(available), topic_count=len(active),
        unclassified_count=len(available - assigned),
        items=[dict(id=t.id, name=t.name, parent_id=t.parent_id, source_count=len(members[t.id]),
                    name_fixed=t.name_fixed, method="local_tag_candidates" if t.automatic_key else "manual") for t in page],
        next_offset=offset + len(page) if offset + len(page) < len(active) else None,
        ai_overview_available=False,
        local_index_status="ready" if state and index and state.generation == index.generation else "pending")


def topic_sources(db: Session, owner: str, domain: str, topic_id: str, offset: int, limit: int) -> dict:
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("Invalid pagination")
    topic = require_topic(db, owner, domain, topic_id)
    query = source_scope(owner, domain, topic.scope).where(Source.id.in_(
        select(DomainTopicMember.source_id).where(DomainTopicMember.topic_id == topic.id,
                                                 DomainTopicMember.included.is_(True))))
    rows = db.scalars(query.order_by(Source.id).offset(offset).limit(limit + 1)).all()
    return dict(items=[dict(id=s.id, title=s.title, revision_id=s.current_revision_id) for s in rows[:limit]],
                next_offset=offset + limit if len(rows) > limit else None)


class TopicCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    parent_id: str | None = Field(default=None, min_length=1, max_length=36)
    scope: DomainScope = "app"

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Topic name cannot be blank")
        return value


class TopicEdit(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    parent_id: str | None = Field(default=None, min_length=1, max_length=36)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        return TopicCreate.clean_name(value) if value is not None else None


class MemberEdit(StrictModel):
    included: bool


def set_parent(db: Session, topic: DomainTopic, parent_id: str | None) -> None:
    if parent_id:
        parent = require_topic(db, topic.owner_id, topic.domain_id, parent_id)
        children = db.scalar(select(DomainTopic.id).where(DomainTopic.parent_id == topic.id).limit(1))
        if parent.id == topic.id or parent.scope != topic.scope or parent.parent_id or children:
            raise ApiError(422, "invalid_parent", "主题限两级，父主题必须属于相同范围且不能形成循环")
    topic.parent_id = parent_id


@router.get("/{domain_id}/topics")
def read_directory(domain_id: str, scope: DomainScope = "app", offset: int = Query(default=0, ge=0),
                   limit: int = Query(default=40, ge=1, le=100),
                   principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    return topic_directory(db, principal.owner_id, domain_id, scope, offset, limit)


@router.post("/{domain_id}/topics/rebuild-local")
def rebuild(domain_id: str, scope: DomainScope = "app", principal: AppPrincipal = Depends(require_app_principal),
            db: Session = Depends(get_db)):
    result = rebuild_local_topics(db, principal.owner_id, domain_id, scope)
    db.commit()
    return result


@router.post("/{domain_id}/topics", status_code=201)
def create_topic(domain_id: str, body: TopicCreate, principal: AppPrincipal = Depends(require_app_principal),
                 db: Session = Depends(get_db)):
    locked_state(db, principal.owner_id, domain_id, body.scope)
    topic = DomainTopic(owner_id=principal.owner_id, domain_id=domain_id, scope=body.scope,
                        name=body.name, name_fixed=True)
    db.add(topic); db.flush()
    set_parent(db, topic, str(body.parent_id) if body.parent_id else None)
    db.commit()
    return {"id": topic.id}


@router.patch("/{domain_id}/topics/{topic_id}")
def edit_topic(domain_id: str, topic_id: str, body: TopicEdit,
               principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    topic = require_topic(db, principal.owner_id, domain_id, topic_id)
    locked_state(db, principal.owner_id, domain_id, topic.scope)
    topic = require_topic(db, principal.owner_id, domain_id, topic_id)
    if "name" in body.model_fields_set:
        if body.name is None:
            raise ApiError(422, "invalid_name", "主题名称不能为空")
        topic.name, topic.name_fixed = body.name, True
    if "parent_id" in body.model_fields_set:
        set_parent(db, topic, str(body.parent_id) if body.parent_id else None)
    db.commit()
    return {"id": topic.id}


@router.put("/{domain_id}/topics/{topic_id}/members/{source_id}")
def edit_member(domain_id: str, topic_id: str, source_id: str, body: MemberEdit,
                principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    topic = require_topic(db, principal.owner_id, domain_id, topic_id)
    locked_state(db, principal.owner_id, domain_id, topic.scope)
    source = db.scalar(source_scope(principal.owner_id, domain_id, topic.scope).where(Source.id == source_id))
    if source is None:
        raise ApiError(404, "not_found", "资料不存在")
    row = db.get(DomainTopicMember, (topic_id, source_id))
    if row is None:
        row = DomainTopicMember(topic_id=topic_id, source_id=source_id)
        db.add(row)
    row.included, row.manual = body.included, True
    db.commit()
    return {"source_id": source_id, "included": body.included}


@router.get("/{domain_id}/topics/{topic_id}/sources")
def read_members(domain_id: str, topic_id: str, offset: int = Query(default=0, ge=0),
                 limit: int = Query(default=40, ge=1, le=100),
                 principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    return topic_sources(db, principal.owner_id, domain_id, topic_id, offset, limit)
