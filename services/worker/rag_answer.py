"""One explicit question, one HTTP attempt; interrupted calls never replay."""
from __future__ import annotations

import json

import httpx
from app.embedded_gateway import gateway_transport
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.ai import canonical, gateway_configured
from app.config import settings
from app.models import EvidenceChunk, RAGJob, RAGTurn, Source
from app.rag import current_evidence
from app.rag_scope import scope_is_current
from app.rag_contract import RAGRequest, RAGResponse, validate_rag_result


def finish(db: Session, job_id: str, status: str, error: str | None, response=None):
    db.rollback()
    job = db.get(RAGJob, job_id)
    if job is None or job.status != "running":
        return
    db.execute(update(RAGTurn).where(RAGTurn.id == job.turn_id).values(status=RAGTurn.status))
    db.expire_all()
    job = db.get(RAGJob, job_id)
    if job is None or job.status != "running":
        db.rollback(); return
    turn = db.get(RAGTurn, job.turn_id)
    scope = json.loads(turn.result_json).get("scope")
    if response:
        for item in json.loads(turn.evidence_json):
            db.execute(update(Source).where(Source.id == item["source_id"]).values(updated_at=Source.updated_at))
        db.expire_all()
        metadata = json.loads(turn.evidence_json)
        if (not all(current_evidence(db, job.owner_id, item) for item in metadata)
                or not scope_is_current(db, job.owner_id, scope, metadata)):
            status, error, response = "cancelled", "资料已修改或删除，回答未发布；此次调用可能已产生用量", None
    if response:
        result = response.result.model_dump()
        result["warnings"] = ["回答仅基于本次检索到的原文节选，不代表整个知识库或完整文档。", *result["warnings"]][:10]
        turn.result_json = canonical({**result, "provider": response.provider,
            "model": response.model, "prompt_version": response.prompt_version, "usage": response.usage.model_dump(),
            **({"scope": scope} if scope else {})})
    job.status = turn.status = status
    turn.last_error = error
    db.commit()


def call_answer(request: RAGRequest, transport=None):
    try:
        with httpx.Client(transport=gateway_transport(transport), timeout=httpx.Timeout(120, connect=10),
                          follow_redirects=False, trust_env=False) as client:
            with client.stream("POST", settings.ai_gateway_url.rstrip("/") + "/v1/answer",
                headers={"Authorization": "Bearer " + settings.ai_gateway_token}, json=request.model_dump()) as reply:
                if reply.status_code != 200:
                    status = "failed" if 400 <= reply.status_code < 500 else "unknown"
                    return status, ("AI 网关拒绝请求，请检查配置" if status == "failed" else
                        "AI 请求结果未知，可能已产生用量；不会自动重试"), None
                content = bytearray()
                for part in reply.iter_bytes():
                    content.extend(part)
                    if len(content) > 256 * 1024:
                        raise ValueError("Response too large")
        response = RAGResponse.model_validate_json(content)
        if response.request_id != request.request_id:
            raise ValueError("Request mismatch")
        validate_rag_result(request, response.result)
        return "succeeded", None, response
    except httpx.HTTPError:
        return "unknown", "AI 请求结果未知，可能已产生用量；不会自动重试", None
    except (ValueError, TypeError):
        return "failed", "AI 回答未通过结构或引用校验，可能已产生用量；不会自动重试", None


def run_rag_once(db: Session, transport=None) -> bool:
    identifier = db.scalar(select(RAGJob.id).where(RAGJob.status == "pending")
                           .order_by(RAGJob.created_at, RAGJob.id).limit(1))
    if identifier is None:
        return False
    claimed = db.execute(update(RAGJob).where(RAGJob.id == identifier, RAGJob.status == "pending")
                         .values(status="running")).rowcount
    if claimed:
        job = db.get(RAGJob, identifier)
        db.execute(update(RAGTurn).where(RAGTurn.id == job.turn_id).values(status="running"))
    db.commit()
    if not claimed:
        return True
    job = db.get(RAGJob, identifier)
    turn = db.get(RAGTurn, job.turn_id) if job else None
    if turn is None:
        return True
    try:
        snapshot = json.loads(job.snapshot_json)
        request = RAGRequest.model_validate(snapshot["request"])
        metadata = json.loads(turn.evidence_json)
        expected = {(item["evidence_id"], item["source_id"], item["revision_id"]) for item in metadata}
        if (request.request_id != job.id or request.question != turn.question or turn.owner_id != job.owner_id
                or {(item.id, item.source_id, item.revision_id) for item in request.evidence} != expected):
            raise ValueError("Snapshot mismatch")
        current = all(current_evidence(db, job.owner_id, item) for item in metadata)
        current = current and scope_is_current(db, job.owner_id, json.loads(turn.result_json).get("scope"), metadata)
        current = current and all(item.text in db.get(EvidenceChunk, item.id).text for item in request.evidence)
    except (ValueError, TypeError, KeyError):
        finish(db, identifier, "failed", "问答快照无效，问题尚未发送")
        return True
    if not current:
        finish(db, identifier, "cancelled", "资料已修改或删除，问题尚未发送")
        return True
    from app.model_configuration import model_binding_matches
    if (not gateway_configured() or snapshot["gateway_url"] != settings.ai_gateway_url.rstrip("/")
            or not model_binding_matches(snapshot, settings)):
        finish(db, identifier, "failed", "网关未配置或已改变，问题尚未发送")
        return True
    db.rollback()
    status, error, response = call_answer(request, transport)
    finish(db, identifier, status, error, response)
    return True


def interrupt_rag(db: Session):
    ids = select(RAGJob.turn_id).where(RAGJob.status == "running")
    db.execute(update(RAGTurn).where(RAGTurn.id.in_(ids)).values(status="unknown",
        last_error="服务中断，回答结果未知，可能已产生用量；不会自动重发"))
    db.execute(update(RAGJob).where(RAGJob.status == "running").values(status="unknown"))
