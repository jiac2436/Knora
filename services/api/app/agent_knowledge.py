"""Read-only Agent views; never expose analysis snapshots or the owner catalog."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import AgentPrincipal, require_agent_principal
from .article_links import article_url
from .database import get_db
from .errors import ApiError
from .schemas import SourceRequest
from .models import AIArtifact, AIJob, Source, EvidenceChunk
from .services import record_audit, require_agent_source, granted_domains
from .summary_index import current_summary_artifact
from .summary_retrieval import source_tags, summary_evidence

router = APIRouter(prefix="/v1/mcp/tools")


def read_agent_knowledge(db: Session, owner_id: str, connection_id: str,
                         source_id: str, tool: str) -> dict:
    started = time.monotonic()
    try:
        source = require_agent_source(db, owner_id, connection_id, source_id)
        if source.processing_status in {"deleting", "deleted"}:
            raise ApiError(404, "not_found", "资料不存在")
        result = {"source_id": source.id, "revision_id": source.current_revision_id,
                  "title": source.title, "app_url": article_url(db, source)}
        if tool == "get_tags":
            result["items"] = source_tags(db, source)
        elif tool == "get_summary":
            artifact = current_summary_artifact(db, source)
            result["available"] = False
            latest_job = db.scalar(select(AIJob).where(
                AIJob.owner_id == owner_id, AIJob.source_id == source.id,
                AIJob.kind == "analyze").order_by(AIJob.created_at.desc(), AIJob.id.desc()).limit(1))
            has_artifact = db.scalar(select(AIArtifact.id).where(
                AIArtifact.owner_id == owner_id, AIArtifact.source_id == source.id).limit(1))
            result["unavailable_reason"] = (
                "analysis_" + latest_job.status if latest_job and latest_job.status != "completed"
                else "no_current_valid_summary" if has_artifact or latest_job else "not_analyzed")
            result["next_action"] = "read_original_evidence"

            if artifact is not None and summary_evidence(db, source, artifact) is not None:
                result.pop("unavailable_reason", None)
                result.pop("next_action", None)
                snapshot = json.loads(artifact.snapshot_json)
                content = json.loads(artifact.result_json)
                entries = {item["id"]: item for item in snapshot["evidence"]}

                def citations(identifiers):
                    return [{"evidence_id": entries[item].get("original_evidence_id") or item,
                             "text_start": entries[item].get("text_start", 0),
                             "text_end": entries[item].get("text_end")}
                            for item in identifiers]

                def point(item):
                    return {"text": item["text"], "citations": citations(item["citations"])}

                result.update(available=True, artifact_id=artifact.id,
                    coverage=snapshot.get("analysis_plan", {}).get("coverage",
                        "partial" if snapshot.get("warnings") else "full"),
                    source_coverage=source.coverage,
                    overview=point(content["overview"]) if content.get("overview") else None,
                    summary=content["summary"],
                    summary_citations=citations(content["summary_citations"]),
                    insights=[point(item) for item in content.get("insights", [])],
                    limitations=[point(item) for item in content.get("limitations", [])])
        else:
            raise ValueError("不支持的 Agent 读取工具")
    except ApiError:
        record_audit(db, owner_id, connection_id, tool, "not_found", [], started)
        raise
    record_audit(db, owner_id, connection_id, tool, "success", [source.id], started)
    return result


@router.post("/get_summary")
def get_summary(payload: SourceRequest,
                principal: AgentPrincipal = Depends(require_agent_principal),
                db: Session = Depends(get_db)) -> dict:
    return read_agent_knowledge(db, principal.owner_id, principal.connection_id,
                                payload.source_id, "get_summary")


@router.post("/get_tags")
def get_tags(payload: SourceRequest,
             principal: AgentPrincipal = Depends(require_agent_principal),
             db: Session = Depends(get_db)) -> dict:
    return read_agent_knowledge(db, principal.owner_id, principal.connection_id,
                                payload.source_id, "get_tags")


class KnowledgeListRequest(BaseModel):
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=20, ge=1, le=20)


def list_agent_knowledge(db: Session, owner_id: str, connection_id: str,
                         payload: KnowledgeListRequest) -> dict:
    started = time.monotonic()
    grants = granted_domains(db, connection_id)
    sources = db.scalars(select(Source).where(
        Source.owner_id == owner_id, Source.domain_id.in_(grants),
        Source.deleted_at.is_(None), Source.visibility != "app_only",
        Source.processing_status.not_in(("deleting", "deleted"))
    ).order_by(Source.created_at, Source.id).offset(payload.offset).limit(payload.limit + 1)).all()
    items = []
    for source in sources[:payload.limit]:
        evidence_ids = list(db.scalars(select(EvidenceChunk.id).where(
            EvidenceChunk.revision_id == source.current_revision_id
        ).order_by(EvidenceChunk.ordinal).limit(3)))
        items.append({"source_id": source.id, "title": source.title,
                      "app_url": article_url(db, source),
                      "coverage": source.coverage, "evidence_ids": evidence_ids})
    record_audit(db, owner_id, connection_id, "list_knowledge", "success",
                 [item["source_id"] for item in items], started)
    return {"items": items, "connection_id": connection_id,
            "authorized_domain_ids": sorted(grants), "truncated": len(sources) > payload.limit,
            "next_offset": payload.offset + payload.limit if len(sources) > payload.limit else None}


@router.post("/list_knowledge")
def list_knowledge(payload: KnowledgeListRequest,
                   principal: AgentPrincipal = Depends(require_agent_principal),
                   db: Session = Depends(get_db)) -> dict:
    return list_agent_knowledge(db, principal.owner_id, principal.connection_id, payload)
