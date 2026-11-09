"""Owner-scoped vocabulary and explicit manual/AI source associations."""
from __future__ import annotations

import unicodedata
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .auth import AppPrincipal, require_app_principal
from .database import get_db
from .errors import ApiError
from .models import KnowledgeTag, KnowledgeTagAlias, Source, SourceTag, TagAssignment, TagSuggestion, User, utcnow

router = APIRouter(prefix="/v1")
Dimension = Literal["topic", "type", "entity"]
Origin = Literal["manual", "ai"]


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTag(Request):
    name: str = Field(max_length=120)
    dimension: Dimension = "topic"
    parent_id: str | None = None


class PatchTag(Request):
    name: str | None = Field(default=None, max_length=120)
    parent_id: str | None = None


class AliasRequest(Request):
    name: str = Field(max_length=120)


class AssignmentRequest(Request):
    tag_id: str


class MergeRequest(Request):
    target_id: str


def normalize(name: str) -> tuple[str, str]:
    display = " ".join(unicodedata.normalize("NFKC", name).split())
    key = display.casefold()
    if not display or len(display) > 120 or len(key) > 160 or any(unicodedata.category(c).startswith("C") for c in display):
        raise ApiError(422, "invalid_tag_name", "标签名称不能为空或包含控制字符")
    return display, key


def lock_owner(db: Session, owner: str) -> None:
    # Serialize cross-table name validation without changing account data.
    db.execute(update(User).where(User.id == owner).values(name=User.name))
    db.expire_all()


def owned_tag(db: Session, owner: str, tag_id: str) -> KnowledgeTag:
    tag = db.scalar(select(KnowledgeTag).where(KnowledgeTag.id == tag_id, KnowledgeTag.owner_id == owner))
    if tag is None:
        raise ApiError(404, "not_found", "标签不存在")
    return tag


def owned_source(db: Session, owner: str, source_id: str) -> Source:
    source = db.scalar(select(Source).where(Source.id == source_id, Source.owner_id == owner,
        Source.deleted_at.is_(None), Source.processing_status.notin_(["deleting", "deleted"])))
    if source is None:
        raise ApiError(404, "not_found", "资料不存在")
    return source


def resolve_name(db: Session, owner: str, dimension: str, key: str) -> KnowledgeTag | None:
    tag = db.scalar(select(KnowledgeTag).where(KnowledgeTag.owner_id == owner,
        KnowledgeTag.dimension == dimension, KnowledgeTag.normalized_name == key))
    if tag:
        return tag
    alias = db.scalar(select(KnowledgeTagAlias).where(KnowledgeTagAlias.owner_id == owner,
        KnowledgeTagAlias.dimension == dimension, KnowledgeTagAlias.normalized_name == key))
    return owned_tag(db, owner, alias.tag_id) if alias else None


def check_parent(db: Session, owner: str, dimension: str, parent_id: str | None, tag_id: str | None = None) -> None:
    if parent_id is None:
        return
    parent = owned_tag(db, owner, parent_id)
    if dimension != "topic" or parent.dimension != "topic" or parent.parent_id is not None or parent.id == tag_id:
        raise ApiError(422, "invalid_tag_parent", "主题标签最多两级，其他维度不支持层级")
    if tag_id and db.scalar(select(KnowledgeTag.id).where(KnowledgeTag.parent_id == tag_id).limit(1)):
        raise ApiError(422, "invalid_tag_parent", "有子标签的主题不能设置父标签")


def tag_view(db: Session, tag: KnowledgeTag) -> dict:
    return {"id": tag.id, "name": tag.name, "dimension": tag.dimension, "parent_id": tag.parent_id,
        "aliases": [{"id": alias.id, "name": alias.name} for alias in db.scalars(
            select(KnowledgeTagAlias).where(KnowledgeTagAlias.tag_id == tag.id,
            KnowledgeTagAlias.owner_id == tag.owner_id).order_by(KnowledgeTagAlias.name))]}


def source_tag_views_many(db: Session, sources: list[Source]) -> dict[str, dict]:
    if not sources:
        return {}
    source_by_id = {source.id: source for source in sources}
    legacy: dict[str, list[str]] = {source.id: [] for source in sources}
    details: dict[str, dict[str, dict]] = {source.id: {} for source in sources}
    for source_id, name in db.execute(select(SourceTag.source_id, SourceTag.name)
        .where(SourceTag.source_id.in_(source_by_id)).order_by(SourceTag.name)):
        legacy[source_id].append(name)
    for tag, assignment in db.execute(select(KnowledgeTag, TagAssignment).join(TagAssignment,
        TagAssignment.tag_id == KnowledgeTag.id).where(TagAssignment.source_id.in_(source_by_id))
        .order_by(KnowledgeTag.name, TagAssignment.origin)):
        source = source_by_id[assignment.source_id]
        if tag.owner_id != source.owner_id:
            continue
        item = details[source.id].setdefault(tag.id, {"id": tag.id, "name": tag.name, "dimension": tag.dimension,
            "origins": [], "stale": True, "assignments": []})
        stale = assignment.origin == "ai" and assignment.revision_id != source.current_revision_id
        item["origins"].append(assignment.origin)
        item["assignments"].append({"origin": assignment.origin, "stale": stale, "revision_id": assignment.revision_id})
        item["stale"] = item["stale"] and stale
    result = {}
    for source in sources:
        values = list(details[source.id].values())
        names = list(dict.fromkeys([item["name"] for item in values if not item["stale"]] + legacy[source.id]))
        result[source.id] = {"tags": names, "tag_details": values, "legacy_rule_tags": legacy[source.id]}
    return result


def source_tag_views(db: Session, source: Source) -> dict:
    return source_tag_views_many(db, [source])[source.id]


@router.get("/tags")
def list_tags(dimension: Dimension | None = None, limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0), principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    query = select(KnowledgeTag).where(KnowledgeTag.owner_id == principal.owner_id)
    if dimension:
        query = query.where(KnowledgeTag.dimension == dimension)
    tags = db.scalars(query.order_by(KnowledgeTag.dimension, KnowledgeTag.name, KnowledgeTag.id).offset(offset).limit(limit + 1)).all()
    return {"items": [tag_view(db, tag) for tag in tags[:limit]], "next_offset": offset + limit if len(tags) > limit else None}


@router.post("/tags")
def create_tag(body: CreateTag, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    lock_owner(db, principal.owner_id)
    name, key = normalize(body.name)
    check_parent(db, principal.owner_id, body.dimension, body.parent_id)
    existing = resolve_name(db, principal.owner_id, body.dimension, key)
    if existing:
        if existing.parent_id != body.parent_id and body.parent_id is not None:
            raise ApiError(409, "tag_conflict", "已有标签的层级不同")
        db.commit()
        return tag_view(db, existing)
    tag = KnowledgeTag(owner_id=principal.owner_id, name=name, normalized_name=key,
        dimension=body.dimension, parent_id=body.parent_id)
    db.add(tag); db.commit()
    return tag_view(db, tag)


@router.patch("/tags/{tag_id}")
def patch_tag(tag_id: str, body: PatchTag, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    lock_owner(db, principal.owner_id)
    tag = owned_tag(db, principal.owner_id, tag_id)
    if "parent_id" in body.model_fields_set:
        check_parent(db, principal.owner_id, tag.dimension, body.parent_id, tag.id)
        tag.parent_id = body.parent_id
    if "name" in body.model_fields_set:
        if body.name is None:
            raise ApiError(422, "invalid_tag_name", "标签名称不能为空")
        name, key = normalize(body.name)
        existing = resolve_name(db, principal.owner_id, tag.dimension, key)
        if existing and existing.id != tag.id:
            raise ApiError(409, "tag_conflict", "名称已被其他标签或别名使用")
        tag.name, tag.normalized_name = name, key
    tag.updated_at = utcnow()
    db.commit()
    return tag_view(db, tag)


@router.post("/tags/{tag_id}/aliases")
def add_alias(tag_id: str, body: AliasRequest, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    lock_owner(db, principal.owner_id)
    tag = owned_tag(db, principal.owner_id, tag_id)
    name, key = normalize(body.name)
    existing = resolve_name(db, principal.owner_id, tag.dimension, key)
    if existing and existing.id != tag.id:
        raise ApiError(409, "tag_conflict", "名称已被其他标签或别名使用")
    if not existing:
        db.add(KnowledgeTagAlias(owner_id=principal.owner_id, tag_id=tag.id, dimension=tag.dimension,
            name=name, normalized_name=key))
    db.commit()
    return tag_view(db, tag)


@router.delete("/tags/{tag_id}/aliases/{alias_id}")
def remove_alias(tag_id: str, alias_id: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    lock_owner(db, principal.owner_id)
    tag = owned_tag(db, principal.owner_id, tag_id)
    db.execute(delete(KnowledgeTagAlias).where(KnowledgeTagAlias.id == alias_id,
        KnowledgeTagAlias.tag_id == tag.id, KnowledgeTagAlias.owner_id == principal.owner_id))
    db.commit()
    return tag_view(db, tag)


@router.get("/sources/{source_id}/tags")
def source_tags(source_id: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    return source_tag_views(db, owned_source(db, principal.owner_id, source_id))


@router.post("/sources/{source_id}/tags")
def assign_tag(source_id: str, body: AssignmentRequest, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    lock_owner(db, principal.owner_id)
    source = owned_source(db, principal.owner_id, source_id)
    tag = owned_tag(db, principal.owner_id, body.tag_id)
    if db.get(TagAssignment, (source.id, tag.id, "manual")) is None:
        db.add(TagAssignment(source_id=source.id, tag_id=tag.id, origin="manual", confirmed_at=utcnow()))
    db.commit()
    return source_tag_views(db, source)


@router.delete("/sources/{source_id}/tags/{tag_id}")
def unassign_tag(source_id: str, tag_id: str, origin: Origin = "manual", principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    lock_owner(db, principal.owner_id)
    source = owned_source(db, principal.owner_id, source_id)
    tag = owned_tag(db, principal.owner_id, tag_id)
    if origin == "ai":
        from .ai_tag_suggestions import reject_tag_name
        reject_tag_name(db, source, tag)
    db.execute(delete(TagAssignment).where(TagAssignment.source_id == source.id,
        TagAssignment.tag_id == tag.id, TagAssignment.origin == origin))
    db.commit()
    return source_tag_views(db, source)


@router.post("/tags/{tag_id}/merge")
def merge_tag(tag_id: str, body: MergeRequest, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    lock_owner(db, principal.owner_id)
    old = owned_tag(db, principal.owner_id, tag_id)
    target = owned_tag(db, principal.owner_id, body.target_id)
    if old.id == target.id:
        db.commit()
        return tag_view(db, target)
    if old.dimension != target.dimension:
        raise ApiError(422, "tag_dimension_mismatch", "只能合并同一维度的标签")
    children = db.scalars(select(KnowledgeTag).where(KnowledgeTag.parent_id == old.id)).all()
    if target.parent_id == old.id or (children and target.parent_id is not None):
        raise ApiError(422, "invalid_tag_parent", "合并会形成循环或超过两级主题")
    for child in children:
        child.parent_id = target.id
    for item in db.scalars(select(TagAssignment).where(TagAssignment.tag_id == old.id)).all():
        existing = db.get(TagAssignment, (item.source_id, target.id, item.origin))
        if existing is None:
            db.add(TagAssignment(source_id=item.source_id, tag_id=target.id, origin=item.origin,
                revision_id=item.revision_id, artifact_id=item.artifact_id, evidence_json=item.evidence_json,
                created_at=item.created_at, confirmed_at=item.confirmed_at))
        elif item.origin == "ai":
            source = db.get(Source, item.source_id)
            if source and existing.revision_id != source.current_revision_id and item.revision_id == source.current_revision_id:
                existing.revision_id, existing.artifact_id, existing.evidence_json = item.revision_id, item.artifact_id, item.evidence_json
        db.delete(item)
    for alias in db.scalars(select(KnowledgeTagAlias).where(KnowledgeTagAlias.tag_id == old.id)).all():
        alias.tag_id = target.id
    db.execute(update(TagSuggestion).where(TagSuggestion.tag_id == old.id,
        TagSuggestion.owner_id == principal.owner_id).values(tag_id=target.id))
    old_name, old_key = old.name, old.normalized_name
    db.flush()
    db.delete(old); db.flush()
    if not resolve_name(db, principal.owner_id, target.dimension, old_key):
        db.add(KnowledgeTagAlias(owner_id=principal.owner_id, tag_id=target.id, dimension=target.dimension,
            name=old_name, normalized_name=old_key))
    target.updated_at = utcnow()
    db.commit()
    return tag_view(db, target)
