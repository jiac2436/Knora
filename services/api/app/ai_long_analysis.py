"""Bounded long-document plans and persisted, non-replayed paid steps."""
from __future__ import annotations

import hashlib
import json
import re
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .ai_contract import AnalysisRequest, GatewayResponse
from .errors import ApiError
from .models import AIJob, AIJobStep, EvidenceChunk, Source

MAX_WIRE_BYTES = 512 * 1024
MAX_SECTIONS = 8
SECTION_CHARS = 24000
SECTION_CHUNKS = 80
FINAL_EXCERPT_CHARS = 500


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def bounded_wire(request: dict) -> dict:
    if len(dump(request).encode("utf-8")) > MAX_WIRE_BYTES:
        raise ApiError(409, "ai_request_too_large", "AI 请求超过传输预算，请减少个人备注或标签目录后重新预览")
    return request


def evidence_plan(db: Session, source: Source) -> tuple[list[dict], list[list[dict]], dict]:
    nonempty = (EvidenceChunk.revision_id == source.current_revision_id, func.length(func.trim(EvidenceChunk.text, " \n\t\r")) > 0)
    count, total = db.execute(select(func.count(), func.coalesce(func.sum(func.length(EvidenceChunk.text)), 0))
        .where(*nonempty)).one()
    short = count <= 80 and total <= 48000
    chunks = db.scalars(select(EvidenceChunk).where(*nonempty).order_by(EvidenceChunk.ordinal)
        .limit(81 if short else MAX_SECTIONS * SECTION_CHUNKS + 1)).all()
    sections, selected, partial = [], [], False
    if short:
        selected = [{"id": chunk.id, "text": chunk.text, "locator_kind": chunk.locator_kind,
            "locator_start": chunk.locator_start, "locator_end": chunk.locator_end} for chunk in chunks]
    else:
        section, chars = [], 0
        for item in derived_evidence(chunks):
            if section and (len(section) >= SECTION_CHUNKS or chars + len(item["text"]) > SECTION_CHARS):
                sections.append(section); section, chars = [], 0
            if len(sections) == MAX_SECTIONS:
                partial = True
                break
            section.append(item); selected.append(item); chars += len(item["text"])
        if section:
            sections.append(section)
    covered = sum(len(item["text"]) for item in selected)
    partial |= covered < total
    return selected, sections, {"coverage": "partial" if partial else "full", "source_coverage": source.coverage,
        "covered_chars": covered, "total_chars": total, "evidence_count": len(selected), "total_evidence_count": count,
        "sections": len(sections), "section_details": [{"index": i + 1, "chars": sum(len(e["text"]) for e in section), "evidence_count": len(section)}
                     for i, section in enumerate(sections)], "calls": len(sections) + 1 if sections else 1}



def derived_evidence(chunks):
    """Derive stable slices without changing original persisted evidence IDs."""
    for chunk in chunks:
        base = {"id": chunk.id, "text": chunk.text, "locator_kind": chunk.locator_kind,
                "locator_start": chunk.locator_start, "locator_end": chunk.locator_end}
        if len(chunk.text) <= SECTION_CHARS:
            yield base
            continue
        for start in range(0, len(chunk.text), SECTION_CHARS):
            end = min(start + SECTION_CHARS, len(chunk.text))
            yield {**base, "id": str(uuid5(NAMESPACE_URL, f"{chunk.id}:{start}:{end}")),
                   "text": chunk.text[start:end], "original_evidence_id": chunk.id,
                   "text_start": start, "text_end": end}


def excerpt(item: dict, text: str) -> dict:
    result = {**item, "text": text}
    if item.get("original_evidence_id") is not None or item.get("text_end") is not None:
        result["text_end"] = item.get("text_start", 0) + len(text)
    return result


def _window_start(text: str, facts: list[str], budget: int) -> int:
    """Locate actual text; memo text is a query, never manufactured evidence."""
    if len(text) <= budget:
        return 0
    folded = text.casefold()
    phrases = [fact.strip().casefold() for fact in facts if fact.strip()]
    tokens = set()
    for phrase in phrases:
        tokens.update(re.findall(r"[a-z0-9][a-z0-9_-]{2,}", phrase))
        for run in re.findall(r"[\u3400-\u9fff]+", phrase):
            tokens.update(run[index:index + 2] for index in range(len(run) - 1))
    tokens = sorted(tokens, key=lambda token: (-len(token), token))[:128]
    anchors = {0, max(0, len(text) - budget)}
    for term in phrases + tokens:
        start = 0
        for _ in range(16):
            position = folded.find(term, start)
            if position < 0:
                break
            anchors.add(max(0, min(len(text) - budget, position - budget // 3)))
            start = position + max(1, len(term))
    def score(start):
        window = folded[start:start + budget]
        # Exact grounded statements outweigh loose lexical similarity.
        return (sum(8 + len(phrase) for phrase in phrases if phrase in window),
                sum(len(token) for token in tokens if token in window), -start)
    return max(anchors, key=score)


def _compact_synthesis_evidence(request: dict, originals: list[dict], memos: list[dict]) -> list[dict]:
    if sum(len(item["text"]) for item in originals) <= 48000:
        full = {**request, "evidence": originals}
        if len(dump(full).encode("utf-8")) <= MAX_WIRE_BYTES:
            return originals
    empty = [{**item, "text": ""} for item in originals]
    # JSON escapes can consume six bytes per character. Reserve metadata first.
    overhead = len(dump({**request, "evidence": empty}).encode("utf-8"))
    available = min(48000, (MAX_WIRE_BYTES - overhead - 1024) // 6)
    if available < len(originals):
        raise ApiError(409, "ai_request_too_large", "合成请求没有足够的原文节选预算，请减少个人备注或标签目录")
    budgets = [0] * len(originals)
    pending = set(range(len(originals)))
    while pending:
        share = available // len(pending)
        small = [index for index in pending if len(originals[index]["text"]) <= share]
        if not small:
            for index in sorted(pending):
                budgets[index] = share
            break
        for index in small:
            budgets[index] = len(originals[index]["text"])
            available -= budgets[index]
            pending.remove(index)
    facts_by_id = {item["id"]: [] for item in originals}
    for memo in memos:
        statements = [memo["overview"], *memo["insights"], *memo["limitations"],
                      {"text": memo["summary"], "citations": memo["summary_citations"]}]
        for statement in statements:
            for identifier in statement["citations"]:
                facts_by_id[identifier].append(statement["text"])
    selected = []
    for item, budget in zip(originals, budgets, strict=True):
        start = _window_start(item["text"], facts_by_id[item["id"]], budget)
        end = min(len(item["text"]), start + budget)
        offset = item.get("text_start", 0)
        selected.append({**item, "text": item["text"][start:end],
            "original_evidence_id": item.get("original_evidence_id") or item["id"],
            "text_start": offset + start, "text_end": offset + end})
    return selected

def wire_from_snapshot(snapshot: dict) -> dict:
    return {key: value for key, value in snapshot.items() if key in AnalysisRequest.model_fields}


def segment_request(snapshot: dict, ordinal: int, request_id: str) -> dict:
    wire = wire_from_snapshot(snapshot)
    wire.update(request_id=request_id, phase="segment", kind="analyze", tags=[], rejected_tags=[], domains=[], projects=[],
                evidence=snapshot["analysis_sections"][ordinal], section_summaries=[])
    if snapshot.get('images'):
        ids = set(snapshot['analysis_image_sections'][ordinal])
        wire['images'] = [image for image in snapshot['images'] if image['id'] in ids]
    return bounded_wire(AnalysisRequest.model_validate(wire).model_dump())


def plan_hash(snapshot: dict) -> str:
    stable = {key: value for key, value in snapshot.items()
              if key not in {"request_id", "request_aliases", "original_plan_evidence", "original_plan_metadata"}}
    if "original_plan_evidence" in snapshot:
        stable["evidence"] = snapshot["original_plan_evidence"]
    if "original_plan_metadata" in snapshot:
        for key in ("analysis_plan", "warnings"):
            stable.pop(key, None)
        stable.update(snapshot["original_plan_metadata"])
    return hashlib.sha256(dump(stable).encode()).hexdigest()


def preflight_snapshot(snapshot: dict) -> None:
    sections = snapshot.get("analysis_sections", [])
    if not sections:
        bounded_wire(AnalysisRequest.model_validate(wire_from_snapshot(snapshot)).model_dump())
        return
    for index in range(len(sections)):
        bounded_wire(segment_request(snapshot, index, snapshot["request_id"]))
    # Reserve the worst UTF-8 budget allowed by the bounded section-summary DTO.
    worst_evidence = sorted(snapshot["evidence"], key=lambda item: len(item["id"]), reverse=True)[:80]
    sample_ids = [item["id"] for item in worst_evidence]
    references = sample_ids[:5]
    memos = [{"section_id": index + 1, "overview": {"text": "😀" * 300, "citations": references},
        "summary": "😀" * 2000, "summary_citations": references,
        "insights": [{"text": "😀" * 400, "citations": references} for _ in range(5)],
        "limitations": [{"text": "😀" * 400, "citations": references} for _ in range(3)]}
        for index in range(len(sections))]
    future = wire_from_snapshot(snapshot)
    future.update(phase="synthesis", section_summaries=memos,
        evidence=[excerpt({"original_evidence_id": None, "text_start": 0, "text_end": None, **item},
            "😀" * FINAL_EXCERPT_CHARS) for item in worst_evidence])
    bounded_wire(future)


def initialize_steps(db: Session, job: AIJob, snapshot: dict) -> None:
    for ordinal in range(len(snapshot.get("analysis_sections", [])) + 1):
        step_id = str(uuid5(UUID(job.id), str(ordinal)))
        phase = "segment" if ordinal < len(snapshot["analysis_sections"]) else "synthesis"
        request = segment_request(snapshot, ordinal, step_id) if phase == "segment" else {}
        db.add(AIJobStep(id=step_id, job_id=job.id, ordinal=ordinal, phase=phase, status="pending", request_json=dump(request)))


def synthesis_request(snapshot: dict, steps: list[AIJobStep], request_id: str) -> dict:
    memos, ids = [], set()
    for index, step in enumerate(steps):
        if step.phase != "segment":
            continue
        if step.status != "completed" or not step.response_json:
            raise ValueError("Missing segment checkpoint")
        response = GatewayResponse.model_validate_json(step.response_json)
        result = response.result
        memo = {"section_id": index + 1, "overview": result.overview.model_dump(), "summary": result.summary,
            "summary_citations": result.summary_citations, "insights": [i.model_dump() for i in result.insights],
            "limitations": [i.model_dump() for i in result.limitations]}
        memos.append(memo)
        for refs in [result.summary_citations, result.overview.citations,
                     *(i.citations for i in result.insights), *(i.citations for i in result.limitations)]:
            ids.update(refs)
    if len(ids) > 80:
        raise ValueError("Too many synthesis evidence references")
    originals = [item for item in snapshot["evidence"] if item["id"] in ids]
    if {item["id"] for item in originals} != ids:
        raise ValueError("Missing synthesis original evidence")
    request = wire_from_snapshot(snapshot)
    request.update(request_id=request_id, phase="synthesis", section_summaries=memos)
    if snapshot.get('images'):
        request['images'] = [image for image in snapshot['images'] if image['evidence_id'] in ids]
    if snapshot.get("contract_version", 1) >= 3:
        snapshot.setdefault("original_plan_evidence", snapshot["evidence"])
        snapshot.setdefault("original_plan_metadata", {key: json.loads(dump(snapshot[key]))
            for key in ("analysis_plan", "warnings") if key in snapshot})
        evidence = _compact_synthesis_evidence(request, originals, memos)
        limited = any(len(item["text"]) < len(original["text"]) for item, original in zip(evidence, originals, strict=True))
        if limited:
            warning = "最终合成仅包含按分段结论定位的原文节选；不代表全文，无法覆盖的结论不得仅凭分段摘要当作事实。"
            request["context"] = {**request.get("context", {}), "synthesis_evidence": {
                "coverage": "partial", "covered_chars": sum(len(item["text"]) for item in evidence),
                "selected_original_chars": sum(len(item["text"]) for item in originals), "warning": warning}}
            snapshot["analysis_plan"] = {**snapshot.get("analysis_plan", {}),
                "synthesis_evidence_coverage": "partial"}
            if warning not in snapshot.setdefault("warnings", []):
                snapshot["warnings"].append(warning)
    else:
        evidence = [excerpt(item, item["text"][:FINAL_EXCERPT_CHARS]) for item in originals]
    if {item["id"] for item in evidence} != ids:
        raise ValueError("Missing synthesis original evidence")
    request.update(evidence=evidence)
    bounded_wire(request)
    validated = AnalysisRequest.model_validate(request).model_dump()
    if snapshot.get("contract_version", 1) >= 3:
        # Final artifacts must expose the exact original windows the model saw.
        # Full segment evidence remains in analysis_sections and checkpoints.
        snapshot["evidence"] = validated["evidence"]
    return validated


def run_long_step(db: Session, job_id: str, transport=None) -> bool:
    """Checkpoint one paid call per worker iteration; never replay an attempted step."""
    from sqlalchemy import update
    from .ai import gateway_configured, is_stale
    from .models import AIArtifact, utcnow
    from services.worker import ai_analysis as worker

    job = db.get(AIJob, job_id)
    snapshot = json.loads(job.snapshot_json)
    source = db.get(Source, job.source_id)
    if source is None or is_stale(db, source, snapshot):
        worker._finish(db, job_id, "cancelled", "资料或个人上下文已更新，后续分析已停止；已完成分段保留")
        return True
    steps = db.scalars(select(AIJobStep).where(AIJobStep.job_id == job.id).order_by(AIJobStep.ordinal)).all()
    step = next((item for item in steps if item.status != "completed"), None)
    if step is None and steps and steps[-1].phase == "synthesis":
        try:
            from .ai_contract import validate_result
            final = steps[-1]
            request = AnalysisRequest.model_validate_json(final.request_json)
            response = GatewayResponse.model_validate_json(final.response_json)
            validate_result(request, response.result)
            if request.request_id != final.id or request.source_id != job.source_id or request.revision_id != snapshot["revision_id"]:
                raise ValueError("Saved final binding mismatch")
            snapshot["analysis_request"], snapshot["phase"] = request.model_dump(), "synthesis"
            job.snapshot_json = dump(snapshot)
            response = aggregate_usage(steps, response)
            db.commit()
            worker._finish(db, job_id, "completed", None, response)
        except (ValueError, TypeError):
            worker._finish(db, job_id, "failed", "已保存的合成结果未通过证据校验，未发送新请求")
        return True
    if step is None or step.status != "pending":
        worker._finish(db, job_id, "unknown", "存在已发送且未完成的请求，后续分析停止，请检查检查点")
        return True
    from .model_configuration import model_binding_matches
    if (not gateway_configured(worker.settings) or snapshot.get("gateway_url") != worker.settings.ai_gateway_url.rstrip("/")
            or not model_binding_matches(snapshot, worker.settings)):
        worker._finish(db, job_id, "failed", "AI 网关配置已变更，后续资料未发送；请重新预览分析")
        return True
    try:
        request_dict = (synthesis_request(snapshot, steps, step.id) if step.phase == "synthesis"
                        else json.loads(step.request_json))
        request = AnalysisRequest.model_validate(request_dict)
        bounded_wire(request.model_dump())
        if (request.request_id != step.id or request.source_id != job.source_id or request.revision_id != snapshot["revision_id"]
                or request.phase != step.phase or request.kind != ("analyze" if step.phase == "segment" else job.kind)):
            raise ValueError("Step snapshot mismatch")
    except (ValueError, TypeError, ApiError):
        worker._finish(db, job_id, "failed", "分段或合成请求未通过范围与证据校验，已完成分段保留；未发送新请求")
        return True
    # Persist the exact request before releasing the database transaction for HTTP.
    step_id, phase, ordinal = step.id, step.phase, step.ordinal
    step.request_json, step.status = dump(request.model_dump()), "running"
    if phase == "synthesis":
        snapshot["analysis_request"], snapshot["phase"] = request.model_dump(), "synthesis"
        job.snapshot_json = dump(snapshot)
    db.commit(); db.rollback()
    status, message, response = worker.call_gateway(request, transport, image_files=snapshot.get('image_files', {}))

    # Serialize checkpoint publication against cancellation and source deletion.
    db.rollback()
    job = db.get(AIJob, job_id)
    step = db.get(AIJobStep, step_id)
    if job is None or step is None or step.status != "running":
        db.rollback()
        return True
    source_id, owner_id = job.source_id, job.owner_id
    locked = db.execute(update(Source).where(Source.id == source_id, Source.owner_id == owner_id,
        Source.deleted_at.is_(None), Source.processing_status.not_in(("deleting", "deleted")))
        .values(updated_at=Source.updated_at)).rowcount
    db.expire_all(); job = db.get(AIJob, job_id); step = db.get(AIJobStep, step_id)
    if job is None or step is None or step.status != "running":
        db.rollback()
        return True
    step.status, step.error, step.completed_at = status, message, utcnow()
    if response is not None:
        step.response_json = dump(response.model_dump())
    if locked and response is not None and phase == "segment":
        section_snapshot = {**request.model_dump(), "context": snapshot["context"], "phase": "segment",
            "image_files": {image.id: snapshot.get('image_files', {}).get(image.id) for image in request.images},
            "analysis_plan": {**snapshot["analysis_plan"], "coverage": "partial"}, "section_index": ordinal + 1,
            "warnings": ["这是单个分段的总结，不代表整篇文档；全文总结需要完成最后合成。"]}
        result = response.result.model_dump()
        result["warnings"] = section_snapshot["warnings"] + result["warnings"][:19]
        db.add(AIArtifact(owner_id=owner_id, source_id=source_id, job_id=step_id, revision_id=snapshot["revision_id"],
            snapshot_json=dump(section_snapshot), result_json=dump(result), provider=response.provider, model=response.model,
            prompt_version=response.prompt_version, usage_json=dump(response.usage.model_dump())))
    if job.status != "running":
        db.commit()  # User cancellation retains completed paid checkpoints only.
        return True
    if not locked:
        job.status, job.last_error, job.completed_at = "cancelled", "资料已删除，后续分析和产物发布已停止", utcnow()
        db.commit(); return True
    source = db.get(Source, source_id)
    if is_stale(db, source, snapshot):
        job.status, job.last_error, job.completed_at = "cancelled", "资料或个人上下文已更新，后续分析已停止；已完成分段保留", utcnow()
        db.commit(); return True
    if status != "completed":
        job.status, job.last_error, job.completed_at = status, message, utcnow()
        db.commit(); return True
    if phase == "segment":
        job.status = "pending"
        db.commit(); return True
    # Commit the final checkpoint first. Artifact publication rechecks cancellation.
    # Aggregate all already incurred usage instead of reporting synthesis alone.
    response = aggregate_usage(steps, response)
    db.commit()
    worker._finish(db, job_id, "completed", None, response)
    return True


def aggregate_usage(steps: list[AIJobStep], response: GatewayResponse) -> GatewayResponse:
    usage = response.usage.model_dump()
    for previous in steps:
        if previous.phase == "segment" and previous.response_json:
            incurred = GatewayResponse.model_validate_json(previous.response_json).usage
            usage["prompt_tokens"] += incurred.prompt_tokens
            usage["completion_tokens"] += incurred.completion_tokens
    return response.model_copy(update={"usage": response.usage.model_copy(update=usage)})
