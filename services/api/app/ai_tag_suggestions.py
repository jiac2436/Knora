"""Explicit decisions on AI tag candidates, separate from manual labels."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .auth import AppPrincipal, require_app_principal
from .database import get_db
from .errors import ApiError
from .knowledge_tags import lock_owner, normalize, resolve_name
from .models import AIArtifact, KnowledgeTag, Source, TagAssignment, TagRejection, TagSuggestion, utcnow

router = APIRouter(prefix="/v1")


def rejection_matches(db: Session, source: Source, dimension: str, name: str, tag_id: str | None = None) -> bool:
    _, key = normalize(name)
    resolved = resolve_name(db, source.owner_id, dimension, key)
    ids = {value for value in (tag_id, resolved.id if resolved else None) if value}
    for rejection in db.scalars(select(TagRejection).where(TagRejection.source_id == source.id,
            TagRejection.revision_id == source.current_revision_id, TagRejection.dimension == dimension)):
        if rejection.normalized_name == key or rejection.tag_id in ids:
            return True
        previous = resolve_name(db, source.owner_id, dimension, rejection.normalized_name)
        if previous and previous.id in ids:
            return True
    return False


def reject_tag_name(db: Session, source: Source, tag: KnowledgeTag) -> None:
    """Used when a user removes an already accepted AI association; caller commits."""
    record_rejection(db, source, tag.dimension, tag.name, tag.id)


def record_rejection(db: Session, source: Source, dimension: str, name: str, tag_id: str | None) -> None:
    name, key = normalize(name)
    identity = (source.id, source.current_revision_id, dimension, key)
    if db.get(TagRejection, identity) is None:
        db.add(TagRejection(source_id=source.id, revision_id=source.current_revision_id,
            dimension=dimension, normalized_name=key, tag_id=tag_id))
    db.flush()
    for item in db.scalars(select(TagSuggestion).where(TagSuggestion.source_id == source.id,
            TagSuggestion.revision_id == source.current_revision_id, TagSuggestion.status == "pending")):
        if rejection_matches(db, source, item.dimension, item.name, item.tag_id):
            item.status, item.decided_at = "rejected", utcnow()


def suggestion_view(db: Session, item: TagSuggestion, stale: bool | None = None) -> dict:
    if stale is None:
        from .ai import is_stale, visible_source
        source = visible_source(db, item.owner_id, item.source_id)
        artifact = db.get(AIArtifact, item.artifact_id)
        stale = artifact is None or is_stale(db, source, json.loads(artifact.snapshot_json))
    return {"id": item.id, "tag_id": item.tag_id, "name": item.name, "dimension": item.dimension,
        "reason": item.reason, "citations": json.loads(item.evidence_json), "status": item.status, "stale": stale}


def owned_suggestion(db: Session, owner: str, suggestion_id: str) -> tuple[TagSuggestion, Source, AIArtifact]:
    from .ai import visible_source
    item = db.scalar(select(TagSuggestion).where(TagSuggestion.id == suggestion_id, TagSuggestion.owner_id == owner))
    if item is None:
        raise ApiError(404, "not_found", "标签建议不存在")
    source = visible_source(db, owner, item.source_id)
    artifact = db.scalar(select(AIArtifact).where(AIArtifact.id == item.artifact_id, AIArtifact.owner_id == owner,
        AIArtifact.source_id == source.id))
    if artifact is None:
        raise ApiError(409, "ai_artifact_invalid", "标签建议的分析记录不存在")
    return item, source, artifact


def lock_suggestion(db: Session, owner: str, suggestion_id: str) -> tuple[TagSuggestion, Source, AIArtifact]:
    item, source, _ = owned_suggestion(db, owner, suggestion_id)
    db.execute(update(Source).where(Source.id == source.id).values(updated_at=Source.updated_at))
    lock_owner(db, owner)
    return owned_suggestion(db, owner, suggestion_id)


@router.post("/ai/tag-suggestions/{suggestion_id}/accept")
def accept_suggestion(suggestion_id: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    from .ai import is_stale
    item, source, artifact = lock_suggestion(db, principal.owner_id, suggestion_id)
    if item.status == "accepted":
        db.commit()
        return suggestion_view(db, item)
    if item.status != "pending":
        raise ApiError(409, "ai_tag_rejected", "标签建议已拒绝，请允许重新建议后重新分析")
    snapshot = json.loads(artifact.snapshot_json)
    if item.revision_id != artifact.revision_id or is_stale(db, source, snapshot):
        raise ApiError(409, "ai_artifact_stale", "资料或个人上下文已更新，请重新分析")
    if rejection_matches(db, source, item.dimension, item.name, item.tag_id):
        raise ApiError(409, "ai_tag_rejected", "当前版本已拒绝此标签")
    name, key = normalize(item.name)
    if item.tag_id:
        tag = db.scalar(select(KnowledgeTag).where(KnowledgeTag.id == item.tag_id, KnowledgeTag.owner_id == principal.owner_id))
        frozen = next((t for t in snapshot.get("tags", []) if t["id"] == item.tag_id), None)
        if tag is None or frozen is None or tag.dimension != frozen["dimension"] or tag.name != frozen["name"]:
            raise ApiError(409, "ai_candidate_changed", "标签目录已变更，请重新分析")
    else:
        tag = resolve_name(db, principal.owner_id, item.dimension, key)
        if tag is None:
            if item.dimension == "type":
                raise ApiError(409, "ai_candidate_changed", "内容类型必须使用已有标签")
            tag = KnowledgeTag(owner_id=principal.owner_id, dimension=item.dimension, name=name, normalized_name=key)
            db.add(tag); db.flush()
    assignment = db.get(TagAssignment, (source.id, tag.id, "ai"))
    if assignment is None:
        assignment = TagAssignment(source_id=source.id, tag_id=tag.id, origin="ai")
        db.add(assignment)
    assignment.revision_id, assignment.artifact_id = item.revision_id, item.artifact_id
    assignment.evidence_json, assignment.confirmed_at = item.evidence_json, utcnow()
    item.tag_id, item.status, item.decided_at = tag.id, "accepted", utcnow()
    db.commit()
    return suggestion_view(db, item)


@router.post("/ai/tag-suggestions/{suggestion_id}/reject")
def reject_suggestion(suggestion_id: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    from .ai import is_stale
    item, source, artifact = lock_suggestion(db, principal.owner_id, suggestion_id)
    if item.status == "rejected":
        db.commit()
        return suggestion_view(db, item)
    if item.status != "pending":
        raise ApiError(409, "ai_tag_already_accepted", "已接受的标签请在资料标签中移除")
    if is_stale(db, source, json.loads(artifact.snapshot_json)):
        raise ApiError(409, "ai_artifact_stale", "资料或个人上下文已更新，请重新分析")
    record_rejection(db, source, item.dimension, item.name, item.tag_id)
    db.commit()
    return suggestion_view(db, item)


@router.delete("/sources/{source_id}/ai-tag-rejections")
def clear_rejections(source_id: str, dimension: str | None = Query(None, pattern="^(topic|type|entity)$"),
        name: str | None = None, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    from .ai import visible_source
    source = visible_source(db, principal.owner_id, source_id)
    db.execute(update(Source).where(Source.id == source.id).values(updated_at=Source.updated_at))
    db.expire_all(); source = visible_source(db, principal.owner_id, source_id)
    statement = delete(TagRejection).where(TagRejection.source_id == source.id, TagRejection.revision_id == source.current_revision_id)
    if dimension:
        statement = statement.where(TagRejection.dimension == dimension)
    if name:
        statement = statement.where(TagRejection.normalized_name == normalize(name)[1])
    count = db.execute(statement).rowcount
    db.commit()
    return {"removed": count}
