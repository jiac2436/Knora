"""Local RAG scope resolution; topic membership is a retrieval boundary."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .domain_topics import require_owned_domain, require_topic, source_scope
from .errors import ApiError
from .models import DomainTopic, DomainTopicMember, Source


def scope_value(domain_ids: list[str] | None, topic_id: str | None) -> dict | None:
    domains = sorted(set(domain_ids or []))
    return {"domain_ids": domains, "topic_id": topic_id} if domains or topic_id else None


def resolve_scope(db: Session, owner: str, scope: dict | None) -> frozenset[str] | None:
    if scope is None:
        return None
    domains, topic_id = scope["domain_ids"], scope["topic_id"]
    for domain_id in domains:
        require_owned_domain(db, owner, domain_id)
    if not topic_id:
        return None
    if len(domains) != 1:
        raise ApiError(422, "rag_topic_domain_required", "主题问答必须指定一个知识域")
    domain = domains[0]
    topic = require_topic(db, owner, domain, topic_id)
    if topic.scope != "app":
        raise ApiError(404, "not_found", "主题不存在")
    topics = list(db.execute(select(DomainTopic.id, DomainTopic.parent_id).where(
        DomainTopic.owner_id == owner, DomainTopic.domain_id == domain, DomainTopic.scope == "app")))
    descendants, pending = {topic.id}, {topic.id}
    while pending:
        children = {identifier for identifier, parent in topics if parent in pending} - descendants
        descendants.update(children)
        pending = children
    members = select(DomainTopicMember.source_id).where(
        DomainTopicMember.topic_id.in_(descendants), DomainTopicMember.included.is_(True))
    return frozenset(db.scalars(source_scope(owner, domain, "app").with_only_columns(Source.id)
        .where(Source.id.in_(members))))


def scope_is_current(db: Session, owner: str, scope: dict | None, metadata: list[dict]) -> bool:
    if scope is None:
        return True
    try:
        allowed = resolve_scope(db, owner, scope)
    except (ApiError, KeyError, TypeError):
        return False
    for item in metadata:
        source = db.get(Source, item["source_id"])
        if source is None or source.domain_id not in scope["domain_ids"]:
            return False
        if allowed is not None and source.id not in allowed:
            return False
    return True
