from __future__ import annotations

import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta, timezone

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .auth import AgentPrincipal, AppPrincipal, require_agent_principal, require_app_principal, token_hash
from .account import router as account_router
from .config import settings
from .article_links import article_url, local_library_id, router as article_links_router
from .database import SessionLocal, create_schema, get_db
from .errors import ApiError, api_error_handler, storage_error_handler
from .files import router as files_router
from .capture_queue import router as capture_queue_router
from .ocr import router as ocr_router
from .web import router as web_router
from .images import router as images_router
from .local_trash import router as trash_router
from .domain_trash import router as domain_trash_router
from .ai import router as ai_router
from .model_switch import router as model_configuration_router
from .knowledge_tags import router as tags_router
from .ai_tag_suggestions import router as ai_tags_router
from .models import (
    AgentConnection,
    ConnectionGrant,
    AuditEvent,
    Domain,
    DomainTrash,
    DocumentJob,
    EvidenceChunk,
    ExperienceRecord,
    OCRJob,
    PersonalContext,
    Project,
    Session as UserSession,
    Source,
    SourceProject,
    SourceRevision,
    WebReadJob,
    WebReadPreference,
    User,
    utcnow,
)
from .summary_index import router as summary_index_router
from .agent_knowledge import router as agent_knowledge_router
from .rag import router as rag_router
from .domain_topics import router as domain_topics_router
from .domain_summary_jobs import router as domain_summary_router
from .domain_builds import router as domain_build_router
from .domain_full_analysis import router as domain_full_router
from .domain_library import router as domain_library_router
from . import domain_navigation  # Register owner navigation before including its router.
from .schemas import (
    CaptureCreate,
    ConnectionCreate,
    ConnectionUpdate,
    ContextPatch,
    DevSessionCreate,
    DomainCreate,
    EvidenceRequest,
    ExperienceCreate,
    ProjectCreate,
    SearchRequest,
    SourceRequest,
    SourceVersionsRequest,
    SupplementCreate,
    RevisionCreate,
)
from .services import (
    _create_revision,
    add_experience,
    agent_evidence,
    create_capture,
    create_connection,
    update_connection,
    evidence_view,
    ensure_search_index,
    experience_view,
    granted_domains,
    patch_context,
    record_audit,
    reindex_source,
    require_domain,
    require_project,
    require_agent_source,
    require_source,
    revoke_connection,
    search_knowledge,
    soft_delete_source,
    source_view,
    source_list_views,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    from .local_runtime import service_guard
    with service_guard():
        create_schema()
        with SessionLocal() as db:
            ensure_search_index(db)
            from .tags import ensure_auto_tags
            ensure_auto_tags(db)
            from .semantic import ensure_semantic_index
            ensure_semantic_index(db)
        yield


app = FastAPI(
    title="Personal Knowledge Agent API",
    version="0.1.0-m1",
    lifespan=lifespan,
    description="M1 modular backend. Official MCP SDK stdio server lives in app.mcp_server; /v1/mcp/tools routes are HTTP diagnostics for the same contracts.",
)
app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(OSError, storage_error_handler)
app.add_exception_handler(OperationalError, storage_error_handler)
app.include_router(summary_index_router)
app.include_router(agent_knowledge_router)
app.include_router(article_links_router)
app.include_router(rag_router)
app.include_router(domain_topics_router)
app.include_router(domain_summary_router)
app.include_router(domain_build_router)
app.include_router(domain_full_router)
app.include_router(domain_library_router)
app.include_router(files_router)
app.include_router(capture_queue_router)
app.include_router(ocr_router)
app.include_router(web_router)
app.include_router(images_router)
app.include_router(trash_router)
app.include_router(domain_trash_router)
app.include_router(ai_router)
app.include_router(model_configuration_router)
app.include_router(tags_router)
app.include_router(ai_tags_router)
if settings.environment != "local":
    app.include_router(account_router)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request.state.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": "请求字段不符合接口契约",
            "request_id": request.state.request_id,
            "retryable": False,
            "details": jsonable_encoder(exc.errors()),
        },
    )


@app.get("/health")
def health() -> dict:
    from .kernel_contract import metadata
    return {"status": "ok", "milestone": "M1", "kernel": metadata()}


def create_dev_session(payload: DevSessionCreate, db: Session = Depends(get_db)) -> dict:
    if settings.environment not in {"development", "test"}:
        raise ApiError(404, "not_found", "接口不存在")
    user = db.scalar(select(User).where(User.email == payload.email.casefold()))
    if user is None:
        user = User(email=payload.email.casefold(), name=payload.name)
        db.add(user)
        db.flush()
    token = f"dev_{secrets.token_urlsafe(32)}"
    session = UserSession(
        user_id=user.id,
        token_hash=token_hash(token),
        expires_at=utcnow() + timedelta(days=30),
    )
    db.add(session)
    db.commit()
    return {"access_token": token, "token_type": "bearer", "user": {"id": user.id, "display_name": user.name}}


if settings.environment in {"development", "test"}:
    app.post("/v1/dev/sessions", status_code=201)(create_dev_session)


@app.get("/v1/me")
def me(principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    user = db.get(User, principal.owner_id)
    if user is None:
        raise ApiError(401, "invalid_session", "会话关联账户不存在")
    result = {"id": user.id, "display_name": user.name}
    library_id = local_library_id(db, user.id)
    if library_id:
        result['library_id'] = library_id
    return result


@app.post("/v1/domains", status_code=201)
def create_domain(
    payload: DomainCreate,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    item = Domain(owner_id=principal.owner_id, name=payload.name.strip())
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "domain_name_conflict", "知识域名称已存在")
    db.refresh(item)
    return {"id": item.id, "name": item.name}


@app.get("/v1/domains")
def list_domains(
    principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)
) -> dict:
    items = db.scalars(select(Domain).where(Domain.owner_id == principal.owner_id, ~Domain.id.in_(select(DomainTrash.domain_id))).order_by(Domain.created_at)).all()
    return {"items": [{"id": item.id, "name": item.name} for item in items]}


@app.post("/v1/projects", status_code=201)
def create_project(
    payload: ProjectCreate,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    item = Project(owner_id=principal.owner_id, **payload.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "name": item.name}


@app.get("/v1/projects")
def list_projects(
    principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)
) -> dict:
    items = db.scalars(select(Project).where(Project.owner_id == principal.owner_id).order_by(Project.created_at)).all()
    return {"items": [{"id": item.id, "name": item.name} for item in items]}


@app.post("/v1/captures", status_code=201)
def capture(
    payload: CaptureCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    source, replay = create_capture(db, principal.owner_id, idempotency_key or "", payload)
    if replay:
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
    return source_view(db, source, detail=True)


@app.get("/v1/sources")
def list_sources(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    domain_id: str | None = None,
    project_id: str | None = None,
    principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)
) -> dict:
    if domain_id:
        require_domain(db, principal.owner_id, domain_id)
    if project_id:
        require_project(db, principal.owner_id, project_id)
    statement = select(Source).where(Source.owner_id == principal.owner_id, Source.deleted_at.is_(None))
    if domain_id:
        statement = statement.where(Source.domain_id == domain_id)
    if project_id:
        statement = statement.where(Source.id.in_(
            select(SourceProject.source_id).where(SourceProject.project_id == project_id)
        ))
    page = db.scalars(statement.order_by(Source.created_at.desc(), Source.id.desc())
                      .offset(offset).limit(limit + 1)).all()
    has_more = len(page) > limit
    return {"items": source_list_views(db, page[:limit]),
            "next_offset": offset + limit if has_more else None}


@app.post("/v1/sources/versions")
def source_versions(
    payload: SourceVersionsRequest,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    versions = db.execute(select(
        Source.id, SourceRevision.version, PersonalContext.version,
    ).outerjoin(
        SourceRevision, SourceRevision.id == Source.current_revision_id,
    ).outerjoin(
        PersonalContext, PersonalContext.source_id == Source.id,
    ).where(
        Source.owner_id == principal.owner_id,
        Source.deleted_at.is_(None),
        Source.id.in_(payload.source_ids),
    )).all()
    return {"items": [
        {"source_id": source_id, "revision": revision, "context_version": context_version or 0}
        for source_id, revision, context_version in versions
    ]}


@app.get("/v1/sources/{source_id}")
def get_source(
    source_id: str,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    return source_view(db, require_source(db, principal.owner_id, source_id), detail=True)


@app.post("/v1/sources/{source_id}/web-read", status_code=202)
def read_web_source(
    source_id: str,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    source = require_source(db, principal.owner_id, source_id)
    if source.kind != "url" or not source.raw_url:
        raise ApiError(422, "not_web_link", "该资料不是可读取的网页链接")
    active = db.scalar(select(WebReadJob).where(WebReadJob.source_id == source.id,
                                               WebReadJob.status.in_(["pending", "running"])))
    if active:
        raise ApiError(409, "web_read_in_progress", "该网页正在读取")
    preference = db.get(WebReadPreference, principal.owner_id)
    job = WebReadJob(owner_id=principal.owner_id, source_id=source.id,
                     engine=preference.engine if preference else "legacy",
                     cleaning_enabled=bool(preference and preference.engine == "crawl4ai"
                                           and preference.cleaning_enabled))
    db.add(job)
    source.processing_status = "pending"
    db.commit()
    return {"id": job.id, "status": job.status}


@app.get("/v1/sources/{source_id}/revisions")
def list_source_revisions(
    source_id: str,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    source = require_source(db, principal.owner_id, source_id)
    revisions = db.scalars(
        select(SourceRevision)
        .where(SourceRevision.source_id == source.id)
        .order_by(SourceRevision.version.desc())
    ).all()
    return {"items": [
        {
            "version": revision.version,
            "created_at": revision.created_at,
            "is_current": revision.id == source.current_revision_id,
            "parser_version": revision.parser_version,
            "evidence": [
                {"evidence_id": item.id, "ordinal": item.ordinal,
                 "locator": {"kind": item.locator_kind, "start": item.locator_start, "end": item.locator_end},
                 "preview": item.text[:160]}
                for item in db.scalars(
                    select(EvidenceChunk)
                    .where(EvidenceChunk.revision_id == revision.id)
                    .order_by(EvidenceChunk.ordinal)
                )
            ],
        }
        for revision in revisions
    ]}


@app.post("/v1/sources/{source_id}/revisions", status_code=201)
def create_source_revision(
    source_id: str,
    payload: RevisionCreate,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    source = require_source(db, principal.owner_id, source_id)
    revision = _create_revision(db, source, payload.text)
    revision.parser_version = "user-edit-v1"
    # Installing an extension must not later replace text the user just edited.
    from .runtime_extensions import MANUAL_EDIT_PRESERVED
    for job_type in (DocumentJob, OCRJob):
        for job in db.scalars(select(job_type).where(
                job_type.source_id == source.id, job_type.status == "pending",
                job_type.last_error.like("extension_required:%"))):
            job.status, job.last_error, job.completed_at = "cancelled", MANUAL_EDIT_PRESERVED, utcnow()
            source.processing_status = "usable"
    if source.coverage in {"link_only", "file_only"}:
        source.coverage = "full_text"
    if source.kind == "url":
        source.processing_status = "usable"
    db.flush()
    reindex_source(db, source)
    db.commit()
    db.refresh(source)
    return source_view(db, source, detail=True)


@app.patch("/v1/sources/{source_id}/context")
def update_context(
    source_id: str,
    payload: ContextPatch,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    source = require_source(db, principal.owner_id, source_id)
    return source_view(db, patch_context(db, principal.owner_id, source, payload), detail=True)


@app.post("/v1/sources/{source_id}/experiences", status_code=201)
def create_experience(
    source_id: str,
    payload: ExperienceCreate,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    source = require_source(db, principal.owner_id, source_id)
    return experience_view(add_experience(db, principal.owner_id, source, payload))


@app.post("/v1/sources/{source_id}/supplements", status_code=201)
def add_supplement(
    source_id: str,
    payload: SupplementCreate,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    source = require_source(db, principal.owner_id, source_id)
    current = db.get(SourceRevision, source.current_revision_id) if source.current_revision_id else None
    combined = f"{current.extracted_text}\n\n{payload.text}" if current and current.extracted_text else payload.text
    revision = _create_revision(db, source, combined)
    if source.kind == "file" and db.scalar(
        select(OCRJob.id).where(OCRJob.source_id == source.id).limit(1)
    ):
        revision.parser_version = "pdf-supplement-v1"
    elif source.kind == "file" and db.scalar(
        select(DocumentJob.id).where(DocumentJob.source_id == source.id).limit(1)
    ):
        revision.parser_version = "anydoc-supplement-v1"
    elif source.kind == "url":
        revision.parser_version = "web-supplement-v1"
    source.coverage = "partial_text" if source.coverage in {"link_only", "file_only"} else source.coverage
    active_web_read = source.kind == "url" and db.scalar(
        select(WebReadJob.id).where(WebReadJob.source_id == source.id,
                                    WebReadJob.status.in_(["pending", "running"])).limit(1)
    )
    if not active_web_read:
        source.processing_status = "usable"
    db.flush()
    reindex_source(db, source)
    db.commit()
    db.refresh(source)
    return source_view(db, source, detail=True)


@app.get("/v1/evidence/{evidence_id}")
def get_app_evidence(
    evidence_id: str,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    evidence = db.get(EvidenceChunk, evidence_id)
    if evidence is None:
        raise ApiError(404, "not_found", "证据不存在")
    revision = db.get(SourceRevision, evidence.revision_id)
    if revision is None:
        raise ApiError(404, "not_found", "证据不存在")
    source = require_source(db, principal.owner_id, revision.source_id)
    result = evidence_view(evidence, revision)
    result["source_id"] = source.id
    current = db.get(SourceRevision, source.current_revision_id) if source.current_revision_id else None
    result["has_newer_revision"] = bool(current and current.version > revision.version)
    return result


@app.post("/v1/search")
def search(
    payload: SearchRequest,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    items = search_knowledge(db, principal.owner_id, payload)
    from .semantic import semantic_index_status
    from .summary_index import summary_index_status
    return {"summary_index": summary_index_status(db, principal.owner_id), "items": items, "truncated": len(items) == payload.limit,
            "semantic_index": semantic_index_status(db, principal.owner_id)}


@app.get("/v1/agent-connections")
def list_agent_connections(principal: AppPrincipal = Depends(require_app_principal),
                           db: Session = Depends(get_db)) -> dict:
    connections = db.scalars(select(AgentConnection).where(AgentConnection.owner_id == principal.owner_id)
        .order_by(AgentConnection.created_at.desc(), AgentConnection.id).limit(100)).all()
    from .agent_connections import current_clients, MANAGED_CLIENTS
    current = current_clients(db, principal.owner_id)
    missing = set(current.values()) - {row.id for row in connections}
    if missing:
        connections.extend(db.scalars(select(AgentConnection).where(
            AgentConnection.owner_id == principal.owner_id, AgentConnection.id.in_(missing))))
    items = []
    for connection in connections:
        expires = connection.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        status = connection.status
        if connection.revoked_at is not None:
            status = "revoked"
        elif status == "active" and expires <= utcnow():
            status = "expired"
        elif status == "active" and connection.client_name in MANAGED_CLIENTS and current.get(connection.client_name) != connection.id:
            status = "superseded"
        domains = list(db.scalars(select(ConnectionGrant.domain_id).join(Domain, Domain.id == ConnectionGrant.domain_id)
            .where(ConnectionGrant.connection_id == connection.id, Domain.owner_id == principal.owner_id)
            .order_by(ConnectionGrant.domain_id)))
        items.append({"id": connection.id, "client_name": connection.client_name, "status": status,
            "domain_ids": domains, "created_at": connection.created_at, "expires_at": connection.expires_at,
            "revoked_at": connection.revoked_at})
    return {"items": items}


@app.post("/v1/agent-connections", status_code=201)
def connect_agent(
    payload: ConnectionCreate,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    connection, token = create_connection(db, principal.owner_id, payload)
    return {
        "id": connection.id,
        "client_name": connection.client_name,
        "status": connection.status,
        "expires_at": connection.expires_at,
        "access_token": token,
        "token_display": "仅本次响应显示，请安全保存",
    }


@app.patch("/v1/agent-connections/{connection_id}", status_code=204)
def update_agent_connection(connection_id: str, payload: ConnectionUpdate,
                            principal: AppPrincipal = Depends(require_app_principal),
                            db: Session = Depends(get_db)) -> Response:
    update_connection(db, principal.owner_id, connection_id, payload)
    return Response(status_code=204)


@app.delete("/v1/agent-connections/{connection_id}", status_code=204)
def disconnect_agent(
    connection_id: str,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> Response:
    revoke_connection(db, principal.owner_id, connection_id)
    return Response(status_code=204)


@app.get("/v1/audit-events")
def list_audit_events(
    principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)
) -> dict:
    events = db.scalars(
        select(AuditEvent).where(AuditEvent.owner_id == principal.owner_id).order_by(AuditEvent.created_at.desc()).limit(100)
    ).all()
    return {
        "items": [
            {
                "id": item.id,
                "connection_id": item.connection_id,
                "tool": item.tool,
                "result": item.result,
                "source_ids": item.source_ids.split(",") if item.source_ids else [],
                "duration_ms": item.duration_ms,
                "created_at": item.created_at,
            }
            for item in events
        ]
    }


@app.delete("/v1/sources/{source_id}", status_code=202)
def delete_source(
    source_id: str,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    job = soft_delete_source(db, principal.owner_id, source_id)
    return {"operation_id": job.id, "status": job.status}


@app.get("/v1/operations/{operation_id}")
def get_operation(
    operation_id: str,
    principal: AppPrincipal = Depends(require_app_principal),
    db: Session = Depends(get_db),
) -> dict:
    from .models import DeletionJob

    job = db.scalar(
        select(DeletionJob).where(
            DeletionJob.id == operation_id,
            DeletionJob.owner_id == principal.owner_id,
        )
    )
    if job is None:
        raise ApiError(404, "not_found", "操作不存在")
    return {
        "id": job.id,
        "kind": "source_deletion",
        "source_id": job.source_id,
        "status": job.status,
        "attempts": job.attempts,
        "last_error": job.last_error,
        "created_at": job.created_at,
        "completed_at": job.completed_at,
    }


@app.post("/v1/mcp/tools/search_knowledge")
def mcp_search(
    payload: SearchRequest,
    principal: AgentPrincipal = Depends(require_agent_principal),
    db: Session = Depends(get_db),
) -> dict:
    started = time.monotonic()
    allowed = granted_domains(db, principal.connection_id)
    items = search_knowledge(db, principal.owner_id, payload, allowed_domains=allowed, for_agent=True)
    record_audit(db, principal.owner_id, principal.connection_id, "search_knowledge", "success", [item["source_id"] for item in items], started)
    return {"items": items, "truncated": len(items) == payload.limit}


@app.post("/v1/mcp/tools/get_evidence")
def mcp_get_evidence(
    payload: EvidenceRequest,
    principal: AgentPrincipal = Depends(require_agent_principal),
    db: Session = Depends(get_db),
) -> dict:
    started = time.monotonic()
    try:
        result = agent_evidence(db, principal.owner_id, principal.connection_id, payload.evidence_id, payload.context_window)
    except ApiError:
        record_audit(db, principal.owner_id, principal.connection_id, "get_evidence", "not_found", [], started)
        raise
    record_audit(db, principal.owner_id, principal.connection_id, "get_evidence", "success", [result["source_id"]], started)
    return result


@app.post("/v1/mcp/tools/get_source")
def mcp_get_source(
    payload: SourceRequest,
    principal: AgentPrincipal = Depends(require_agent_principal),
    db: Session = Depends(get_db),
) -> dict:
    started = time.monotonic()
    try:
        source = require_agent_source(db, principal.owner_id, principal.connection_id, payload.source_id)
    except ApiError:
        record_audit(db, principal.owner_id, principal.connection_id, "get_source", "not_found", [], started)
        raise
    revision = db.get(SourceRevision, source.current_revision_id) if source.current_revision_id else None
    result = {
        "source_id": source.id,
        "title": source.title,
        "author": None,
        "source_url": source.raw_url,
        "app_url": article_url(db, source),
        "coverage": source.coverage,
        "revision": revision.version if revision else None,
        "authorized_content_entry": f"/v1/mcp/tools/get_source",
    }
    record_audit(db, principal.owner_id, principal.connection_id, "get_source", "success", [source.id], started)
    return result


@app.get("/v1/runtime-extensions")
def runtime_extension_status(principal: AppPrincipal = Depends(require_app_principal)):
    from .runtime_extensions import status
    return {"extensions": status(), "cloud_ai": "bundled"}
