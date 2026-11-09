"""Explicit knowledge questions, frozen original evidence and local conversations."""
from __future__ import annotations

import hashlib
import json
import re
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .ai import canonical, gateway_configured
from .auth import AppPrincipal, require_app_principal
from .config import settings
from .database import get_db
from .errors import ApiError
from .models import EvidenceChunk, RAGJob, RAGSession, RAGTurn, Source, SourceRevision, new_id
from .rag_contract import RAGEvidence, RAGRequest
from .rag_scope import resolve_scope, scope_is_current, scope_value
from .schemas import SearchRequest
from .services import search_knowledge

router = APIRouter(prefix="/v1/rag")


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="知识库问答", min_length=1, max_length=200)


class TurnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID
    question: str = Field(min_length=1, max_length=2000)
    confirm_send: bool = False
    domain_ids: list[str] | None = Field(default=None, max_length=50)
    project_id: str | None = None
    tag_query: str | None = Field(default=None, max_length=120)
    topic_id: UUID | None = None


def owned_session(db: Session, owner: str, identifier: str) -> RAGSession:
    session = db.scalar(select(RAGSession).where(RAGSession.id == identifier, RAGSession.owner_id == owner))
    if session is None:
        raise ApiError(404, "not_found", "问答会话不存在")
    return session


def current_evidence(db: Session, owner: str, item: dict) -> bool:
    source = db.get(Source, item["source_id"])
    chunk = db.get(EvidenceChunk, item.get("evidence_id", item.get("id")))
    revision = db.get(SourceRevision, item["revision_id"])
    return bool(source and source.owner_id == owner and source.deleted_at is None
        and source.processing_status not in {"deleting", "deleted"}
        and source.current_revision_id == item["revision_id"] and revision and revision.source_id == source.id
        and chunk and chunk.revision_id == revision.id)


def display_answer(answer: str | None, citations: list[dict]) -> str | None:
    """Render legacy evidence markers without changing stored text or bindings."""
    if answer is None:
        return None
    numbers = {}
    for index, item in enumerate(citations):
        for key in ('evidence_id', 'source_id', 'revision_id'):
            if item.get(key):
                numbers.setdefault(item[key].lower(), index + 1)
    identifier = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
    answer = re.sub(r'\[(' + identifier + r')\]', lambda match: f'[{numbers[match[1].lower()]}]'
                    if match[1].lower() in numbers else match[0], answer)
    # Also handle a provider spelling an internal ID without brackets. UUIDs
    # from the actual subject matter remain untouched unless they are bindings.
    return re.sub(identifier, lambda match: f'[{numbers[match[0].lower()]}]'
                  if match[0].lower() in numbers else match[0], answer)


def turn_view(db: Session, turn: RAGTurn) -> dict:
    result = json.loads(turn.result_json)
    metadata = json.loads(turn.evidence_json)
    cited = set(result.get("citations", []))
    scope_current = scope_is_current(db, turn.owner_id, result.get("scope"), metadata)
    citations = [{**item, "stale": not scope_current or not current_evidence(db, turn.owner_id, item)}
                 for item in metadata if item["evidence_id"] in cited]
    status, error = turn.status, turn.last_error
    if status in {"pending", "running"} and db.scalar(select(RAGJob.id).where(RAGJob.turn_id == turn.id)) is None:
        status, error = "unknown", "历史任务已中断或从备份恢复；不会自动重发模型请求"
    return {"id": turn.id, "session_id": turn.session_id, "question": turn.question,
            "status": status, "last_error": error, "answer": display_answer(result.get("answer"), citations),
            "citations": citations, "insufficient_evidence": result.get("insufficient_evidence", False),
            "warnings": result.get("warnings", []), "created_at": turn.created_at, "scope": result.get("scope")}


def freeze_evidence(db: Session, owner: str, body: TurnCreate):
    from .runtime_extensions import available
    if not available("semantic"):
        raise ApiError(409, "extension_required", "知识库问答需要语义搜索扩展，请先安装并重新打开客户端")
    scope = scope_value(body.domain_ids, str(body.topic_id) if body.topic_id else None)
    allowed = resolve_scope(db, owner, scope)
    if allowed is not None and not allowed:
        return [], []
    options = {"allowed_source_ids": allowed} if allowed is not None else {}
    hits = search_knowledge(db, owner, SearchRequest(query=body.question[:1000], limit=12,
        domain_ids=body.domain_ids, project_id=body.project_id, tag_query=body.tag_query), **options)
    evidence, metadata, remaining = [], [], 18000
    for item in hits:
        if remaining <= 0:
            break
        chunk = db.get(EvidenceChunk, item["evidence_id"])
        source = db.get(Source, item["source_id"])
        if not source or not chunk:
            continue
        db.execute(update(Source).where(Source.id == source.id).values(updated_at=Source.updated_at))
        db.expire(source)
        meta = {"evidence_id": chunk.id, "source_id": source.id, "revision_id": chunk.revision_id,
                "title": source.title[:200], "locator_kind": chunk.locator_kind,
                "locator_start": chunk.locator_start, "locator_end": chunk.locator_end}
        if (not current_evidence(db, owner, meta)
                or (scope and source.domain_id not in scope["domain_ids"])
                or (allowed is not None and source.id not in allowed)
                or chunk.id in {entry.id for entry in evidence}):
            continue
        match = chunk.text.find(item["snippet"])
        start = max(0, match - 200) if match >= 0 else 0
        text = chunk.text[start:start + min(3000, remaining)]
        if not text.strip():
            continue
        if chunk.locator_kind == "text":
            meta.update(locator_start=chunk.locator_start + start, locator_end=chunk.locator_start + start + len(text))
        wire = RAGEvidence(id=chunk.id, text=text, **{k: v for k, v in meta.items() if k != "evidence_id"})
        if item.get("knowledge_node"):
            node = item["knowledge_node"]
            # Local provenance only: the answer gateway still receives originals.
            meta["knowledge_node"] = {key: node[key] for key in
                ("artifact_id", "node_id", "domain_id", "kind", "coverage", "role")}
            meta["match_sources"] = item["match_sources"]
        evidence.append(wire)
        metadata.append(meta)
        remaining -= len(text)
    return evidence, metadata


@router.get("/config")
def config(principal: AppPrincipal = Depends(require_app_principal)) -> dict:
    return {"configured": gateway_configured()}


def matching_scope(scope: dict | None):
    if scope is None:
        return func.json_extract(RAGTurn.result_json, "$.scope").is_(None)
    return and_(func.json_extract(RAGTurn.result_json, "$.scope.domain_ids") == canonical(scope["domain_ids"]),
                func.json_extract(RAGTurn.result_json, "$.scope.topic_id") == scope["topic_id"])


@router.get("/sessions")
def list_sessions(principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db),
                  domain_id: str | None = None, topic_id: str | None = None) -> dict:
    scope = scope_value([domain_id] if domain_id else None, topic_id)
    resolve_scope(db, principal.owner_id, scope)
    history = select(RAGTurn.id).where(RAGTurn.owner_id == principal.owner_id, RAGTurn.session_id == RAGSession.id)
    included = history.where(matching_scope(scope)).exists()
    if scope is None:
        included = included | ~history.exists()
    rows = db.scalars(select(RAGSession).where(RAGSession.owner_id == principal.owner_id, included)
                      .order_by(RAGSession.created_at.desc(), RAGSession.id).limit(100))
    return {"items": [{"id": row.id, "title": row.title, "created_at": row.created_at} for row in rows]}


@router.post("/sessions", status_code=201)
def create_session(body: SessionCreate, principal: AppPrincipal = Depends(require_app_principal),
                   db: Session = Depends(get_db)) -> dict:
    row = RAGSession(owner_id=principal.owner_id, title=body.title)
    db.add(row); db.commit()
    return {"id": row.id, "title": row.title, "created_at": row.created_at}


@router.get("/turns")
def list_turns(session_id: str, limit: int = Query(100, ge=1, le=100),
               principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db),
               domain_id: str | None = None, topic_id: str | None = None) -> dict:
    owned_session(db, principal.owner_id, session_id)
    scope = scope_value([domain_id] if domain_id else None, topic_id)
    resolve_scope(db, principal.owner_id, scope)
    rows = list(db.scalars(select(RAGTurn).where(RAGTurn.session_id == session_id, RAGTurn.owner_id == principal.owner_id)
                          .where(matching_scope(scope))
                          .order_by(RAGTurn.created_at.desc(), RAGTurn.id.desc()).limit(limit)))
    return {"items": [turn_view(db, row) for row in reversed(rows)]}


@router.post("/turns", status_code=202)
def create_turn(body: TurnCreate, idempotency_key: str = Header(alias="Idempotency-Key"),
                principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)) -> dict:
    from .model_switch import require_accepting
    from .model_configuration import frozen_model_binding
    try:
        key = str(UUID(idempotency_key))
    except ValueError:
        raise ApiError(422, "invalid_request_key", "问答请求标识必须为 UUID") from None
    if not body.question.strip():
        raise ApiError(422, "empty_question", "请输入问题")
    if not body.confirm_send:
        raise ApiError(409, "rag_confirmation_required", "请确认检索到的原文将发送给 AI 网关并可能消耗额度")
    payload = body.model_dump(mode="json")
    if body.topic_id is None:
        payload.pop("topic_id")  # Preserve request hashes from earlier clients.
    digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
    owned_session(db, principal.owner_id, str(body.session_id))
    existing = db.scalar(select(RAGTurn).where(RAGTurn.owner_id == principal.owner_id, RAGTurn.request_key == key))
    if existing:
        if existing.request_hash != digest:
            raise ApiError(409, "rag_request_conflict", "请求标识已用于其他问题")
        return turn_view(db, existing)
    require_accepting(db)
    db.execute(update(RAGSession).where(RAGSession.id == str(body.session_id)).values(created_at=RAGSession.created_at))
    if db.scalar(select(RAGJob.id).join(RAGTurn, RAGTurn.id == RAGJob.turn_id).where(
            RAGTurn.session_id == str(body.session_id), RAGJob.status.in_(["pending", "running"]))) is not None:
        raise ApiError(409, "rag_session_busy", "该会话已有问题处理中，请完成后再提交")
    evidence, metadata = freeze_evidence(db, principal.owner_id, body)
    if evidence and not gateway_configured():
        raise ApiError(503, "ai_not_configured", "请先配置 AI 网关")
    turn = RAGTurn(owner_id=principal.owner_id, session_id=str(body.session_id), question=body.question,
                   request_key=key, request_hash=digest, evidence_json=canonical(metadata), status="pending")
    scope = scope_value(body.domain_ids, str(body.topic_id) if body.topic_id else None)
    if scope:
        turn.result_json = canonical({"scope": scope})
    db.add(turn); db.flush()
    if evidence:
        request = RAGRequest(request_id=new_id(), question=body.question, evidence=evidence)
        db.add(RAGJob(id=request.request_id, owner_id=principal.owner_id, turn_id=turn.id,
            snapshot_json=canonical({"request": request.model_dump(), "gateway_url": settings.ai_gateway_url.rstrip("/"),
                                     **frozen_model_binding(settings)})))
    else:
        turn.status = "succeeded"
        turn.result_json = canonical({"answer": "当前检索范围没有找到可用原文证据，无法据此回答。",
            "citations": [], "insufficient_evidence": True, "warnings": ["没有发起模型请求；可尝试调整问题或筛选范围。"],
            **({"scope": scope} if scope else {})})
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(RAGTurn).where(RAGTurn.owner_id == principal.owner_id, RAGTurn.request_key == key))
        if existing is None or existing.request_hash != digest:
            raise ApiError(409, "rag_request_conflict", "请求标识已用于其他问题")
        turn = existing
    return turn_view(db, turn)


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, principal: AppPrincipal = Depends(require_app_principal),
                   db: Session = Depends(get_db)) -> dict:
    owned_session(db, principal.owner_id, session_id)
    db.execute(delete(RAGJob).where(RAGJob.turn_id.in_(select(RAGTurn.id).where(RAGTurn.session_id == session_id))))
    db.execute(delete(RAGTurn).where(RAGTurn.session_id == session_id))
    db.execute(delete(RAGSession).where(RAGSession.id == session_id))
    db.commit()
    return {"deleted": True}


def purge_source_turns(db: Session, source_id: str) -> None:
    """Erasing source knowledge also erases derived conversation answers and jobs."""
    for turn in db.scalars(select(RAGTurn)).all():
        if any(item["source_id"] == source_id for item in json.loads(turn.evidence_json)):
            db.execute(delete(RAGJob).where(RAGJob.turn_id == turn.id))
            db.delete(turn)
    db.flush()
