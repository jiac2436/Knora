from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .config import settings


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Library(Base):
    __tablename__ = "libraries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="本地知识库")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LocalInstallation(Base):
    __tablename__ = "local_installations"
    __table_args__ = (CheckConstraint("id = 1", name="ck_local_installation_singleton"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(String(36), unique=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    library_id: Mapped[str] = mapped_column(ForeignKey("libraries.id"))
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReplicaState(Base):
    __tablename__ = "replica_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_replica_singleton"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    library_id: Mapped[str] = mapped_column(String(36))
    device_id: Mapped[str] = mapped_column(String(36))
    epoch: Mapped[str | None] = mapped_column(String(36))
    applied_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    protocol_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")


class SyncEntityState(Base):
    __tablename__ = "sync_entities"
    __table_args__ = (CheckConstraint("generation >= acknowledged_generation AND acknowledged_generation >= 0 AND cloud_version >= 0", name="ck_sync_entity_versions"),)
    entity_kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer)
    acknowledged_generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cloud_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class LocalChange(Base):
    __tablename__ = "local_changes"
    __table_args__ = (CheckConstraint("change_type IN ('upsert', 'delete')", name="ck_local_change_type"), {"sqlite_autoincrement": True})
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_kind: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[str] = mapped_column(String(36))
    generation: Mapped[int] = mapped_column(Integer)
    device_id: Mapped[str] = mapped_column(String(36))
    change_type: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SyncOperation(Base):
    __tablename__ = "sync_operations"
    __table_args__ = (CheckConstraint("status IN ('prepared', 'acknowledged')", name="ck_sync_operation_status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    library_id: Mapped[str] = mapped_column(String(36))
    device_id: Mapped[str] = mapped_column(String(36))
    entity_kind: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[str] = mapped_column(String(36))
    generation: Mapped[int] = mapped_column(Integer)
    base_version: Mapped[int] = mapped_column(Integer)
    payload_json: Mapped[str] = mapped_column(Text)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="prepared", server_default="prepared")
    cloud_version: Mapped[int | None] = mapped_column(Integer)


def initial_upload_status() -> str:
    return "local_saved" if settings.environment == "local" else "cloud_saved"


class PasswordCredential(Base):
    __tablename__ = "password_credentials"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountToken(Base):
    __tablename__ = "account_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(20))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Domain(Base):
    __tablename__ = "domains"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_domain_owner_name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DomainTrash(Base):
    """Retain the domain and the exact set of sources deleted with it."""
    __tablename__ = "domain_trash"
    domain_id: Mapped[str] = mapped_column(ForeignKey("domains.id"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source_ids_json: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    goal: Mapped[str | None] = mapped_column(Text)
    constraints: Mapped[str | None] = mapped_column(Text)
    background: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DomainProcessingPreference(Base):
    __tablename__ = "domain_processing_preferences"
    __table_args__ = (CheckConstraint("scope IN ('app', 'shareable')", name="ck_domain_preference_scope"),)
    domain_id: Mapped[str] = mapped_column(ForeignKey("domains.id"), primary_key=True)
    scope: Mapped[str] = mapped_column(String(20), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    policy_json: Mapped[str] = mapped_column(Text)
    authorization_id: Mapped[str] = mapped_column(String(36))


class DomainProcessingState(Base):
    """Disposable input generation; policy is stored separately for backups."""
    __tablename__ = "domain_processing_states"
    __table_args__ = (
        CheckConstraint("scope IN ('app', 'shareable')", name="ck_domain_state_scope"),
        CheckConstraint("generation >= 0", name="ck_domain_generation"),
        CheckConstraint("used_calls >= 0 AND used_input_tokens >= 0", name="ck_domain_usage"),
        CheckConstraint("phase IN ('not_built','dirty','preparing','generating','indexing','ready','partial','failed','unknown')", name="ck_domain_phase"),
    )
    domain_id: Mapped[str] = mapped_column(ForeignKey("domains.id"), primary_key=True)
    scope: Mapped[str] = mapped_column(String(20), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    dirty_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    phase: Mapped[str] = mapped_column(String(30), default="not_built", server_default="not_built")
    first_build_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    used_calls: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    used_input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    request_id: Mapped[str | None] = mapped_column(String(36))


class DomainTopic(Base):
    __tablename__ = "domain_topics"
    __table_args__ = (
        UniqueConstraint("domain_id", "scope", "automatic_key", name="uq_domain_topic_key"),
        CheckConstraint("scope IN ('app','shareable')", name="ck_domain_topic_scope"),
        CheckConstraint("parent_id IS NULL OR parent_id != id", name="ck_topic_parent"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    domain_id: Mapped[str] = mapped_column(ForeignKey("domains.id"), index=True)
    scope: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(120))
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("domain_topics.id"))
    automatic_key: Mapped[str | None] = mapped_column(String(128))
    name_fixed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class DomainTopicMember(Base):
    __tablename__ = "domain_topic_members"
    topic_id: Mapped[str] = mapped_column(ForeignKey("domain_topics.id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    included: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class DomainTopicIndexState(Base):
    __tablename__ = "domain_topic_index_states"
    domain_id: Mapped[str] = mapped_column(ForeignKey("domains.id"), primary_key=True)
    scope: Mapped[str] = mapped_column(String(20), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer)


class DomainBuild(Base):
    """Frozen bounded tree recipe; child calls use existing durable node jobs."""
    __tablename__ = "domain_builds"
    __table_args__ = (
        CheckConstraint("scope IN ('app','shareable')", name="ck_domain_build_scope"),
        CheckConstraint("status IN ('pending','running','completed','unknown','failed','stale','cancelled')", name="ck_domain_build_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    domain_id: Mapped[str] = mapped_column(ForeignKey("domains.id"), index=True)
    scope: Mapped[str] = mapped_column(String(20))
    generation: Mapped[int] = mapped_column(Integer)
    authorization_id: Mapped[str] = mapped_column(String(36))
    gateway_url: Mapped[str] = mapped_column(Text)
    plan_json: Mapped[str] = mapped_column(Text)
    confirmed_unknown: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DomainFullAnalysisJob(Base):
    """One owner-confirmed domain workflow; no runnable work survives restore."""
    __tablename__ = "domain_full_analysis_jobs"
    __table_args__ = (
        CheckConstraint("status IN ('pending','running','cancelling','completed','partial','failed','unknown','stale','cancelled')", name="ck_full_analysis_status"),
        CheckConstraint("phase IN ('documents','overview','indexing','completed')", name="ck_full_analysis_phase"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    domain_id: Mapped[str] = mapped_column(ForeignKey("domains.id"), index=True)
    request_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    phase: Mapped[str] = mapped_column(String(20), default="documents")
    snapshot_json: Mapped[str] = mapped_column(Text)
    progress_json: Mapped[str] = mapped_column(Text, default="{}")
    budget_json: Mapped[str] = mapped_column(Text)
    build_id: Mapped[str | None] = mapped_column(String(36))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DomainSummaryJob(Base):
    """Durable single-attempt checkpoint; never restored as runnable work."""
    __tablename__ = "domain_summary_jobs"
    __table_args__ = (
        CheckConstraint("scope IN ('app','shareable')", name="ck_node_job_scope"),
        CheckConstraint("status IN ('pending','running','completed','failed','unknown','stale','cancelled')", name="ck_node_job_status"),
        CheckConstraint("generation >= 0 AND input_tokens > 0", name="ck_node_job_budget"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    domain_id: Mapped[str] = mapped_column(ForeignKey("domains.id"), index=True)
    scope: Mapped[str] = mapped_column(String(20))
    generation: Mapped[int] = mapped_column(Integer)
    authorization_id: Mapped[str] = mapped_column(String(36))
    snapshot_json: Mapped[str] = mapped_column(Text)
    gateway_url: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer)
    confirmed_unknown: Mapped[bool] = mapped_column(Boolean, default=False)
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DomainSummaryArtifact(Base):
    """Immutable result and exact input dependencies; serving rechecks inputs."""
    __tablename__ = "domain_summary_artifacts"
    __table_args__ = (CheckConstraint("scope IN ('app','shareable')", name="ck_node_artifact_scope"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    domain_id: Mapped[str] = mapped_column(ForeignKey("domains.id"), index=True)
    scope: Mapped[str] = mapped_column(String(20))
    node_id: Mapped[str] = mapped_column(String(36), index=True)
    snapshot_json: Mapped[str] = mapped_column(Text)
    response_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DomainNodeIndexJob(Base):
    """Disposable local embedding job; no paid provider requests."""
    __tablename__ = 'domain_node_index_jobs'
    artifact_id: Mapped[str] = mapped_column(ForeignKey('domain_summary_artifacts.id', ondelete='CASCADE'), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str] = mapped_column(String(120))
    text_sha256: Mapped[str] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(String(200))


class DomainNodeEmbedding(Base):
    """Immutable-artifact-bound, rebuildable node vector windows."""
    __tablename__ = 'domain_node_embeddings'
    artifact_id: Mapped[str] = mapped_column(ForeignKey('domain_summary_artifacts.id', ondelete='CASCADE'), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    model: Mapped[str] = mapped_column(String(120))
    text_sha256: Mapped[str] = mapped_column(String(64))
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    domain_id: Mapped[str] = mapped_column(ForeignKey("domains.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(500))
    raw_input: Mapped[str | None] = mapped_column(Text)
    raw_url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text, index=True)
    visibility: Mapped[str] = mapped_column(String(40), default="app_and_agents")
    coverage: Mapped[str] = mapped_column(String(40))
    upload_status: Mapped[str] = mapped_column(String(40), default=initial_upload_status)
    processing_status: Mapped[str] = mapped_column(String(40), default="usable")
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_kind: Mapped[str] = mapped_column(String(40), default="mobile")
    client_version: Mapped[str] = mapped_column(String(80), default="unknown")
    current_revision_id: Mapped[str | None] = mapped_column(String(36))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    revisions: Mapped[list["SourceRevision"]] = relationship(back_populates="source")


class RAGSession(Base):
    __tablename__ = "rag_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RAGTurn(Base):
    __tablename__ = "rag_turns"
    __table_args__ = (UniqueConstraint("owner_id", "request_key", name="uq_rag_turn_owner_request"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("rag_sessions.id"), index=True)
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="pending", server_default="pending")
    last_error: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    evidence_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    request_key: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RAGJob(Base):
    __tablename__ = "rag_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("rag_turns.id"), unique=True)
    snapshot_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AIJob(Base):
    __tablename__ = "ai_jobs"
    __table_args__ = (
        UniqueConstraint("owner_id", "request_key", name="uq_ai_job_owner_request"),
        CheckConstraint("status IN ('pending', 'running', 'completed', 'failed', 'unknown', 'cancelled')", name="ck_ai_job_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    request_key: Mapped[str] = mapped_column(String(36))
    request_sha256: Mapped[str] = mapped_column(String(64))
    snapshot_json: Mapped[str] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AIJobStep(Base):
    __tablename__ = "ai_job_steps"
    __table_args__ = (
        UniqueConstraint("job_id", "ordinal", name="uq_ai_job_step_ordinal"),
        CheckConstraint("phase IN ('segment', 'synthesis')", name="ck_ai_job_step_phase"),
        CheckConstraint("status IN ('pending', 'running', 'completed', 'failed', 'unknown')", name="ck_ai_job_step_status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("ai_jobs.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    request_json: Mapped[str] = mapped_column(Text, default="{}")
    response_json: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AIArtifact(Base):
    __tablename__ = "ai_artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    # Jobs are operational state and are excluded from portable backups.
    job_id: Mapped[str] = mapped_column(String(36), unique=True)
    revision_id: Mapped[str] = mapped_column(String(36))
    result_json: Mapped[str] = mapped_column(Text)
    snapshot_json: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(80))
    usage_json: Mapped[str] = mapped_column(Text)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FileAsset(Base):
    __tablename__ = "file_assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), unique=True)
    filename: Mapped[str] = mapped_column(String(500))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))


class SourceProject(Base):
    __tablename__ = "source_projects"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), primary_key=True)


class KnowledgeTag(Base):
    __tablename__ = "knowledge_tags"
    __table_args__ = (
        UniqueConstraint("owner_id", "dimension", "normalized_name", name="uq_knowledge_tag_name"),
        CheckConstraint("dimension IN ('topic', 'type', 'entity')", name="ck_knowledge_tag_dimension"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    dimension: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(120))
    normalized_name: Mapped[str] = mapped_column(String(160))
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_tags.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KnowledgeTagAlias(Base):
    __tablename__ = "knowledge_tag_aliases"
    __table_args__ = (UniqueConstraint("owner_id", "dimension", "normalized_name", name="uq_knowledge_tag_alias_name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    tag_id: Mapped[str] = mapped_column(ForeignKey("knowledge_tags.id"), index=True)
    dimension: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(120))
    normalized_name: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TagAssignment(Base):
    __tablename__ = "tag_assignments"
    __table_args__ = (CheckConstraint("origin IN ('ai', 'manual')", name="ck_tag_assignment_origin"),)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    tag_id: Mapped[str] = mapped_column(ForeignKey("knowledge_tags.id"), primary_key=True)
    origin: Mapped[str] = mapped_column(String(20), primary_key=True)
    revision_id: Mapped[str | None] = mapped_column(String(36))
    artifact_id: Mapped[str | None] = mapped_column(String(36))
    evidence_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TagSuggestion(Base):
    __tablename__ = "tag_suggestions"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'accepted', 'rejected')", name="ck_tag_suggestion_status"),
        CheckConstraint("dimension IN ('topic', 'type', 'entity')", name="ck_tag_suggestion_dimension"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    artifact_id: Mapped[str] = mapped_column(String(36), index=True)
    revision_id: Mapped[str] = mapped_column(String(36))
    tag_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_tags.id"))
    name: Mapped[str] = mapped_column(String(120))
    normalized_name: Mapped[str] = mapped_column(String(160))
    dimension: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TagRejection(Base):
    __tablename__ = "tag_rejections"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dimension: Mapped[str] = mapped_column(String(20), primary_key=True)
    normalized_name: Mapped[str] = mapped_column(String(160), primary_key=True)
    tag_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceTag(Base):
    __tablename__ = "source_tags"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    name: Mapped[str] = mapped_column(String(40), primary_key=True)


class SourceTagState(Base):
    __tablename__ = "source_tag_state"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    revision_id: Mapped[str | None] = mapped_column(String(36))
    rule_version: Mapped[int] = mapped_column(Integer)


class SourceRevision(Base):
    __tablename__ = "source_revisions"
    __table_args__ = (UniqueConstraint("source_id", "version", name="uq_source_revision_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    extracted_text: Mapped[str] = mapped_column(Text)
    parser_version: Mapped[str] = mapped_column(String(80), default="m1-direct-input-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[Source] = relationship(back_populates="revisions")
    evidence: Mapped[list["EvidenceChunk"]] = relationship(back_populates="revision")


class EvidenceChunk(Base):
    __tablename__ = "evidence_chunks"
    __table_args__ = (UniqueConstraint("revision_id", "ordinal", name="uq_evidence_revision_ordinal"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    locator_kind: Mapped[str] = mapped_column(String(30), default="text")
    locator_start: Mapped[int] = mapped_column(Integer, default=0)
    locator_end: Mapped[int] = mapped_column(Integer)
    revision: Mapped[SourceRevision] = relationship(back_populates="evidence")


class SourceSearchDocument(Base):
    """Local cache identity: UUIDs are stored once per indexed document."""
    __tablename__ = "source_search_documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), unique=True)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)


class SourceSearchTerm(Base):
    __tablename__ = "source_search_terms"
    __table_args__ = (Index("ix_source_search_terms_term", "term"), {"sqlite_with_rowid": False})
    document_id: Mapped[int] = mapped_column(ForeignKey("source_search_documents.id"), primary_key=True)
    term: Mapped[str] = mapped_column(String(128), primary_key=True)


class SemanticIndexJob(Base):
    __tablename__ = "semantic_index_jobs"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class SummaryIndexJob(Base):
    __tablename__ = "summary_index_jobs"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    artifact_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class SummarySearchTerm(Base):
    __tablename__ = "summary_search_terms"
    __table_args__ = (Index("ix_summary_terms_owner_term", "owner_id", "term"),)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("ai_artifacts.id"), primary_key=True)
    term: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))


class SummaryEmbedding(Base):
    __tablename__ = "summary_embeddings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    artifact_id: Mapped[str] = mapped_column(ForeignKey("ai_artifacts.id"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(120))
    vector: Mapped[bytes] = mapped_column(LargeBinary)


class EvidenceEmbedding(Base):
    __tablename__ = "evidence_embeddings"
    __table_args__ = (Index("ix_evidence_embeddings_owner_source", "owner_id", "source_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id"))
    evidence_id: Mapped[str] = mapped_column(ForeignKey("evidence_chunks.id"))
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(120))
    vector: Mapped[bytes] = mapped_column(LargeBinary)


class PersonalContext(Base):
    __tablename__ = "personal_context"
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), primary_key=True)
    reason: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    applicability: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ExperienceRecord(Base):
    __tablename__ = "experience_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"))
    result: Mapped[str] = mapped_column(String(40))
    notes: Mapped[str | None] = mapped_column(Text)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentConnection(Base):
    __tablename__ = "agent_connections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    client_name: Mapped[str] = mapped_column(String(160))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConnectionGrant(Base):
    __tablename__ = "connection_grants"
    connection_id: Mapped[str] = mapped_column(ForeignKey("agent_connections.id"), primary_key=True)
    domain_id: Mapped[str] = mapped_column(ForeignKey("domains.id"), primary_key=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    connection_id: Mapped[str | None] = mapped_column(ForeignKey("agent_connections.id"), index=True)
    tool: Mapped[str] = mapped_column(String(80))
    result: Mapped[str] = mapped_column(String(40))
    source_ids: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("owner_id", "client_request_id", name="uq_idempotency_owner_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    client_request_id: Mapped[str] = mapped_column(String(200))
    payload_hash: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeletionJob(Base):
    __tablename__ = "deletion_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OCRPreference(Base):
    __tablename__ = "ocr_preferences"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    model: Mapped[str] = mapped_column(String(20), default="small")
    engine: Mapped[str] = mapped_column(String(20), default="pdfium", server_default="pdfium")


class OCRJob(Base):
    __tablename__ = "ocr_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    model: Mapped[str] = mapped_column(String(20))
    engine: Mapped[str] = mapped_column(String(20), default="pdfium", server_default="pdfium")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentJob(Base):
    __tablename__ = "document_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebReadJob(Base):
    __tablename__ = "web_read_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    engine: Mapped[str] = mapped_column(String(20), default="legacy", server_default="legacy")
    cleaning_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebReadPreference(Base):
    __tablename__ = "web_read_preferences"
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    engine: Mapped[str] = mapped_column(String(20), default="legacy", server_default="legacy")
    cleaning_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class WebReadMarkdown(Base):
    __tablename__ = "web_read_markdown"
    revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id"), primary_key=True)
    markdown: Mapped[str] = mapped_column(Text)


class FeishuDocumentArtifact(Base):
    __tablename__ = "feishu_document_artifacts"
    revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id"), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(200))
    document_revision_id: Mapped[int] = mapped_column(Integer)
    cli_version: Mapped[str] = mapped_column(String(80))


class SourceImage(Base):
    __tablename__ = "source_images"
    __table_args__ = (UniqueConstraint("revision_id", "ordinal", name="uq_source_image_ordinal"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("source_revisions.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    block_id: Mapped[str] = mapped_column(String(200), default="")
    caption: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    failure_reason: Mapped[str] = mapped_column(String(500), default="")
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    retrieval_method: Mapped[str] = mapped_column(String(20), default="download", server_default="download")
    file_key: Mapped[str | None] = mapped_column(String(36))
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    mime_type: Mapped[str | None] = mapped_column(String(40))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
