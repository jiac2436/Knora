"""Local submission snapshots; no network dispatch or cloud entitlement implied."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import (DomainTrash, Domain, Project, Source, SourceProject, SourceRevision, EvidenceChunk,
                     FileAsset, PersonalContext, ExperienceRecord, SourceImage,
                     WebReadMarkdown, FeishuDocumentArtifact, LocalInstallation,
                     ReplicaState, SyncEntityState, SyncOperation, AIArtifact,
                     KnowledgeTag, KnowledgeTagAlias, TagAssignment, TagSuggestion, TagRejection, RAGSession, RAGTurn,
                     DomainProcessingPreference, DomainTopic, DomainTopicMember, DomainSummaryArtifact)

ENTITY_MODELS = {"domain": Domain, "project": Project, "source": Source, "tag": KnowledgeTag, "rag_session": RAGSession}


def record(row) -> dict:
    result = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        result[column.name] = value.isoformat() if isinstance(value, datetime) else value
    return result


def replica(db: Session) -> ReplicaState:
    if db.get_bind().dialect.name != "sqlite":
        raise ValueError("提交记录仅用于已准备的本地 SQLite 库")
    state = db.get(ReplicaState, 1)
    installation = db.get(LocalInstallation, 1)
    if state is None or installation is None or (state.library_id, state.device_id) != (
            installation.library_id, installation.device_id):
        raise ValueError("本地同步身份尚未准备或不一致")
    return state


def lock_entity(db: Session, kind: str, identifier: str) -> SyncEntityState:
    if kind not in ENTITY_MODELS or not isinstance(identifier, str) or not identifier:
        raise ValueError("同步实体类型或编号无效")
    db.flush()
    # Serialize snapshot creation/acknowledgement and subsequent business writes.
    changed = db.execute(update(SyncEntityState).where(
        SyncEntityState.entity_kind == kind, SyncEntityState.entity_id == identifier)
        .values(generation=SyncEntityState.generation)
        .execution_options(synchronize_session=False)).rowcount
    if changed != 1:
        raise ValueError("实体没有本地变更记录")
    db.expire_all()
    state = db.get(SyncEntityState, (kind, identifier))
    db.refresh(state)
    return state


def dependency(db: Session, kind: str, identifier: str, owner: str, binding: ReplicaState) -> dict:
    row = db.get(ENTITY_MODELS[kind], identifier)
    if row is None or row.owner_id != owner:
        raise ValueError("同步实体的知识域或项目依赖缺失")
    state = db.get(SyncEntityState, (kind, identifier))
    operation = db.scalar(select(SyncOperation).where(
        SyncOperation.library_id == binding.library_id, SyncOperation.device_id == binding.device_id,
        SyncOperation.entity_kind == kind, SyncOperation.entity_id == identifier,
        SyncOperation.status == "prepared"))
    if state is None or (state.cloud_version == 0 and operation is None):
        raise ValueError("请先准备知识域和项目的提交")
    return {"entity_kind": kind, "entity_id": identifier,
            "operation_id": operation.id if operation else None}


def snapshot_entity(db: Session, kind: str, identifier: str, binding: ReplicaState) -> dict:
    installation = db.get(LocalInstallation, 1)
    row = db.get(ENTITY_MODELS[kind], identifier)
    if row is not None and row.owner_id != installation.owner_id:
        raise ValueError("实体不属于当前本地知识库")
    if kind == "domain" and (trashed := db.get(DomainTrash, identifier)) is not None:
        return {"deleted": True, "permanent": False,
                "entity": {"id": identifier, "deleted_at": trashed.deleted_at.isoformat()}, "dependencies": []}
    if row is None or (kind == "source" and row.deleted_at is not None):
        return {"deleted": True, "permanent": row is None or row.processing_status in {"deleting", "deleted"},
                "entity": {"id": identifier, "deleted_at": row.deleted_at.isoformat() if row is not None else None},
                "dependencies": []}
    if kind == "rag_session":
        turns = db.scalars(select(RAGTurn).where(
            RAGTurn.session_id == identifier, RAGTurn.owner_id == installation.owner_id)
            .order_by(RAGTurn.created_at, RAGTurn.id)).all()
        return {"deleted": False, "entity": record(row), "turns": [record(turn) for turn in turns],
                "dependencies": []}
    if kind == "tag":
        dependencies = [dependency(db, "tag", row.parent_id, row.owner_id, binding)] if row.parent_id else []
        aliases = db.scalars(select(KnowledgeTagAlias).where(
            KnowledgeTagAlias.tag_id == identifier, KnowledgeTagAlias.owner_id == installation.owner_id)
            .order_by(KnowledgeTagAlias.id)).all()
        return {"deleted": False, "entity": record(row), "aliases": [record(alias) for alias in aliases],
                "dependencies": dependencies}
    if kind == "domain":
        preferences = db.scalars(select(DomainProcessingPreference).where(
            DomainProcessingPreference.domain_id == identifier,
            DomainProcessingPreference.owner_id == installation.owner_id).order_by(DomainProcessingPreference.scope)).all()
        topics = db.scalars(select(DomainTopic).where(DomainTopic.domain_id == identifier,
            DomainTopic.owner_id == installation.owner_id).order_by(DomainTopic.id)).all()
        members = db.scalars(select(DomainTopicMember).where(DomainTopicMember.topic_id.in_([t.id for t in topics]))
            .order_by(DomainTopicMember.topic_id, DomainTopicMember.source_id)).all()
        artifacts = db.scalars(select(DomainSummaryArtifact).where(DomainSummaryArtifact.domain_id == identifier,
            DomainSummaryArtifact.owner_id == installation.owner_id).order_by(DomainSummaryArtifact.id)).all()
        return {"node_artifacts": [record(a) for a in artifacts], "deleted": False, "entity": record(row), "processing_preferences": [record(p) for p in preferences],
                "topics": [record(t) for t in topics], "topic_members": [record(m) for m in members], "dependencies": []}
    if kind != "source":
        return {"deleted": False, "entity": record(row), "dependencies": []}
    links = db.scalars(select(SourceProject).where(SourceProject.source_id == identifier)
                       .order_by(SourceProject.project_id)).all()
    dependencies = [dependency(db, "domain", row.domain_id, row.owner_id, binding)]
    experience_projects = db.scalars(select(ExperienceRecord.project_id).where(
        ExperienceRecord.source_id == identifier, ExperienceRecord.owner_id == installation.owner_id,
        ExperienceRecord.project_id.is_not(None))).all()
    project_ids = sorted({link.project_id for link in links} | set(experience_projects))
    dependencies += [dependency(db, "project", project_id, row.owner_id, binding) for project_id in project_ids]
    tag_ids = set(db.scalars(select(TagAssignment.tag_id).where(TagAssignment.source_id == identifier)))
    tag_ids |= set(db.scalars(select(TagSuggestion.tag_id).where(TagSuggestion.source_id == identifier,
        TagSuggestion.owner_id == installation.owner_id, TagSuggestion.tag_id.is_not(None))))
    dependencies += [dependency(db, "tag", tag_id, row.owner_id, binding) for tag_id in sorted(tag_ids)]
    revisions = db.scalars(select(SourceRevision).where(SourceRevision.source_id == identifier)
                           .order_by(SourceRevision.version, SourceRevision.id)).all()
    if row.current_revision_id is not None and row.current_revision_id not in {item.id for item in revisions}:
        raise ValueError("资料当前版本关联不完整")
    revision_ids = [item.id for item in revisions]
    body = {"deleted": False, "entity": record(row), "dependencies": dependencies,
            "projects": [record(link) for link in links], "revisions": []}
    for name in ("processing_status", "upload_status"):
        body["entity"].pop(name, None)
    for revision in revisions:
        value = record(revision)
        value["origin_display_version"] = value.pop("version")
        body["revisions"].append(value)
    children = (("evidence", EvidenceChunk, EvidenceChunk.revision_id.in_(revision_ids)),
                ("files", FileAsset, FileAsset.source_id == identifier),
                ("context", PersonalContext, PersonalContext.source_id == identifier),
                ("experiences", ExperienceRecord, (ExperienceRecord.source_id == identifier) &
                 (ExperienceRecord.owner_id == installation.owner_id)),
                ("images", SourceImage, SourceImage.source_id == identifier),
                ("web_markdown", WebReadMarkdown, WebReadMarkdown.revision_id.in_(revision_ids)),
                ("feishu_artifacts", FeishuDocumentArtifact, FeishuDocumentArtifact.revision_id.in_(revision_ids)),
                ("ai_artifacts", AIArtifact, (AIArtifact.source_id == identifier) &
                 (AIArtifact.owner_id == installation.owner_id)),
                ("tag_assignments", TagAssignment, TagAssignment.source_id == identifier),
                ("tag_suggestions", TagSuggestion, (TagSuggestion.source_id == identifier) &
                 (TagSuggestion.owner_id == installation.owner_id)),
                ("tag_rejections", TagRejection, TagRejection.source_id == identifier))
    for name, model, clause in children:
        rows = db.scalars(select(model).where(clause).order_by(*model.__table__.primary_key.columns)).all()
        body[name] = [record(item) for item in rows]
    # Diagnostics are not knowledge and may contain provider details or credentials.
    for image in body["images"]:
        image["failure_reason"] = ""
    return body


def prepare_operation(db: Session, entity_kind: str, entity_id: str) -> SyncOperation:
    """Caller commits; repeated preparation reuses the outstanding immutable snapshot."""
    binding = replica(db)
    state = lock_entity(db, entity_kind, entity_id)
    existing = db.scalar(select(SyncOperation).where(
        SyncOperation.library_id == binding.library_id, SyncOperation.device_id == binding.device_id,
        SyncOperation.entity_kind == entity_kind, SyncOperation.entity_id == entity_id,
        SyncOperation.status == "prepared"))
    if existing is not None:
        verify_payload(existing)
        return existing
    if state.generation <= state.acknowledged_generation:
        raise ValueError("实体没有待提交修改")
    payload = {"protocol_version": 1, "library_id": binding.library_id,
               "entity_kind": entity_kind, "entity_id": entity_id,
               "body": snapshot_entity(db, entity_kind, entity_id, binding)}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    operation = SyncOperation(id=str(uuid.uuid4()), library_id=binding.library_id,
                              device_id=binding.device_id, entity_kind=entity_kind, entity_id=entity_id,
                              generation=state.generation, base_version=state.cloud_version,
                              payload_json=encoded, payload_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                              status="prepared")
    db.add(operation)
    db.flush()
    return operation


def verify_payload(operation: SyncOperation) -> None:
    if hashlib.sha256(operation.payload_json.encode()).hexdigest() != operation.payload_sha256:
        raise ValueError("固定提交载荷摘要不一致")


def acknowledge_operation(db: Session, operation_id: str, cloud_version: int, payload_sha256: str) -> dict:
    """Confirm one snapshot without discarding edits made after it was prepared."""
    binding = replica(db)
    operation = db.get(SyncOperation, operation_id)
    if operation is None or (operation.library_id, operation.device_id) != (binding.library_id, binding.device_id):
        raise ValueError("提交记录不存在或不属于当前设备")
    state = lock_entity(db, operation.entity_kind, operation.entity_id)
    db.refresh(operation)
    verify_payload(operation)
    if payload_sha256 != operation.payload_sha256 or type(cloud_version) is not int or cloud_version != operation.base_version + 1:
        raise ValueError("提交确认版本或载荷摘要不一致")
    if operation.status == "acknowledged":
        if operation.cloud_version != cloud_version:
            raise ValueError("重复确认的版本不一致")
    elif operation.status == "prepared":
        if state.cloud_version != operation.base_version or state.generation < operation.generation:
            raise ValueError("提交基础版本或本地代次已不一致")
        state.cloud_version = cloud_version
        state.acknowledged_generation = max(state.acknowledged_generation, operation.generation)
        operation.status = "acknowledged"
        operation.cloud_version = cloud_version
        db.flush()
    else:
        raise ValueError("提交状态不允许确认")
    return {"id": operation.id, "status": operation.status, "cloud_version": operation.cloud_version,
            "dirty": state.generation > state.acknowledged_generation}
