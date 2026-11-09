"""Bounded provider calls and output validation; no HTTP server ownership."""
import json
import logging
from typing import Any

import httpx
from pydantic import ValidationError

from app.ai_contract import AnalysisRequest, AnalysisResult, AnalysisUsage, GatewayResponse, validate_result
from app.rag_contract import RAGRequest, RAGResult, RAGResponse, validate_rag_result
from app.domain_contract import (summary_provider_input, reading_budget, DomainSummaryRequest,
    DomainSummaryResult, DomainSummaryResponse, validate_domain_summary)
from .errors import ModelExecutionError
from .settings import GatewaySettings
from .prompts import (prompt_for, domain_prompt_for, SUMMARY_PROMPT_VERSION, DOMAIN_PROMPT_VERSION,
    RAG_SYSTEM_PROMPT, RAG_PROMPT_VERSION)

MAX_RESPONSE_BYTES = 256 * 1024


class CompactJSONError(ValueError):
    def __init__(self, category: str, fields=()):
        super().__init__("Ambiguous compact JSON")
        self.category = category
        safe_names = set(AnalysisResult.model_fields) | {"section_id", "section_index", "phase", "source_id",
            "revision_id", "request_id", "result", "analysis", "metadata", "title", "text", "citations",
            "key_points", "scope", "coverage", "version", "content"}
        self.fields = sorted(set(fields) & safe_names)


def ordered_domain_request(snapshot: DomainSummaryRequest) -> DomainSummaryRequest:
    """Keep a document's antecedents adjacent to their following statements.

    Selection may be balanced across sources, but the provider's reading order
    must not interleave unrelated documents. IDs, text and offsets are intact.
    """
    evidence = sorted(snapshot.evidence, key=lambda e: (
        e.source_id, e.locator_start or 0, e.original_evidence_id or e.id,
        e.text_start, e.id))
    return snapshot.model_copy(update=dict(evidence=evidence))


async def provider_failure(response):
    """Classify a bounded error; never return/log the provider's message."""
    data = bytearray()
    async for part in response.aiter_bytes():
        if len(data) + len(part) > 4096:
            break
        data.extend(part)
    try:
        error = json.loads(data).get('error', {})
        message = str(error.get('message', '')).casefold() if isinstance(error, dict) else ''
    except (ValueError, TypeError, AttributeError):
        message = ''
    category = 'unclassified'
    for needle, name in [('image', 'image_input_rejected'), ('response_format', 'response_format_rejected'),
            ('thinking', 'thinking_option_rejected'), ('balance', 'quota_or_balance'),
            ('quota', 'quota_or_balance'), ('api key', 'provider_auth'), ('api_key', 'provider_auth'),
            ('model', 'model_configuration')]:
        if needle in message:
            category = name
            break
    logging.getLogger(__name__).warning('AI provider rejected request: HTTP %d, category=%s', response.status_code, category)
    return ModelExecutionError(502, detail={'code': 'provider_failed', 'upstream_status': response.status_code,
        'category': category, 'message': '大模型服务暂时无法完成请求，请检查网关配置或服务状态。'})


async def _provider_call(settings: GatewaySettings, snapshot: AnalysisRequest | RAGRequest | DomainSummaryRequest, transport: httpx.AsyncBaseTransport | None) -> GatewayResponse | RAGResponse | DomainSummaryResponse:
    if isinstance(snapshot, DomainSummaryRequest):
        provider_snapshot = ordered_domain_request(snapshot)
        system_prompt, prompt_version = domain_prompt_for(provider_snapshot), (SUMMARY_PROMPT_VERSION if snapshot.contract_version == 2 else DOMAIN_PROMPT_VERSION)
    else:
        provider_snapshot = snapshot
        system_prompt, prompt_version = (RAG_SYSTEM_PROMPT, RAG_PROMPT_VERSION) if isinstance(snapshot, RAGRequest) else prompt_for(snapshot)
    content = (json.dumps(summary_provider_input(provider_snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if isinstance(snapshot, DomainSummaryRequest) and snapshot.contract_version == 2
        else provider_snapshot.model_dump_json(exclude_defaults=True))
    model = settings.model
    if isinstance(snapshot, AnalysisRequest) and snapshot.images:
        from .vision import user_content
        if snapshot.phase != 'synthesis':
            if not settings.effective_vision_model:
                raise ModelExecutionError(400, detail={'code': 'vision_model_not_configured',
                    'message': '请配置支持图片的 PKA_AI_VISION_MODEL，系统不会退回仅正文分析。'})
            model = settings.effective_vision_model
        content = user_content(snapshot, snapshot.model_dump)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        "response_format": {"type": "json_object"},
        "stream": False,
        # Even a short navigation result can contain up to seventy UUIDs.
        # Allow the citation envelope; text still has its own 500/800 ceiling.
        "max_tokens": 8192 if isinstance(snapshot, DomainSummaryRequest) and snapshot.contract_version == 2 else 16384 if isinstance(snapshot, DomainSummaryRequest) else 2048 if (isinstance(snapshot, AnalysisRequest) and snapshot.contract_version == 3 and snapshot.phase != "segment") else 8192,
    }
    if isinstance(snapshot, DomainSummaryRequest) and snapshot.contract_version == 2:
        payload["thinking"] = {"type": "disabled"}
    elif isinstance(snapshot, DomainSummaryRequest):
        # Multi-case attribution needs reasoning; allow its tokens separately
        # from the short final JSON. Never persist or return reasoning_content.
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = "high"
    elif isinstance(snapshot, AnalysisRequest) and snapshot.contract_version == 3:
        payload["thinking"] = {"type": "disabled"}
    if settings.provider != "deepseek":
        payload.pop("thinking", None)
        payload.pop("reasoning_effort", None)
    stage = "provider_envelope"
    result = None
    try:
        async with httpx.AsyncClient(transport=transport, timeout=settings.timeout_seconds, follow_redirects=False, trust_env=False) as client:
            async with client.stream(
                "POST", settings.base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {settings.provider_key}"}, json=payload,
            ) as response:
                if response.status_code != 200:
                    raise await provider_failure(response)
                content = bytearray()
                async for part in response.aiter_bytes():
                    if len(content) + len(part) > MAX_RESPONSE_BYTES:
                        raise ModelExecutionError(502, detail={"code": "provider_response_too_large", "message": "大模型返回内容超出允许范围。"})
                    content.extend(part)
        data = json.loads(content)
        if not isinstance(data, dict) or not isinstance(data.get("choices"), list):
            raise ValueError("Invalid provider envelope")
        choice = data["choices"][0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise ValueError("Invalid provider choice")
        stage = "finish_reason"
        if choice.get("finish_reason") != "stop":
            raise ValueError("Provider output was not completed")
        stage = "output_structure"
        if isinstance(snapshot, DomainSummaryRequest):
            result = DomainSummaryResult.model_validate_json(choice["message"]["content"])
            stage = "evidence_validation"
            validate_domain_summary(snapshot, result)
        elif isinstance(snapshot, RAGRequest):
            result = RAGResult.model_validate_json(choice["message"]["content"])
            stage = "evidence_validation"
            validate_rag_result(snapshot, result)
        else:
            raw_result = choice["message"]["content"]
            if snapshot.contract_version == 3:
                try:
                    normalized = normalize_compact_json(raw_result)
                except json.JSONDecodeError:
                    # Preserve safe parser diagnostics for syntax we cannot normalize.
                    result = AnalysisResult.model_validate_json(raw_result)
                else:
                    result = AnalysisResult.model_validate(normalized)
            else:
                result = AnalysisResult.model_validate_json(raw_result)
            stage = "evidence_validation"
            validate_result(snapshot, result)
        stage = "usage"
        usage = data["usage"]
        if not isinstance(usage, dict):
            raise ValueError("Invalid provider usage")
        response_type = DomainSummaryResponse if isinstance(snapshot, DomainSummaryRequest) else RAGResponse if isinstance(snapshot, RAGRequest) else GatewayResponse
        extra = {"request_id": snapshot.request_id} if isinstance(snapshot, (RAGRequest, DomainSummaryRequest)) else {}
        return response_type(
            **extra, result=result, provider=settings.provider, model=model, prompt_version=prompt_version,
            usage=AnalysisUsage(prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"]),
        )
    except httpx.TimeoutException:
        raise ModelExecutionError(504, detail={"code": "provider_result_unknown", "message": "大模型请求超时，可能已产生用量；当前请求不会自动重发。"}) from None
    except httpx.HTTPError:
        raise ModelExecutionError(502, detail={"code": "provider_result_unknown", "message": "大模型连接中断，可能已产生用量；当前请求不会自动重发。"}) from None
    except (ValueError, TypeError, KeyError, IndexError, ValidationError) as error:
        reasons = {
            "Provider output was not completed": "output_incomplete",
            "Compact overview and insights exceed reading budget": "reading_budget",
            "Compact summary or insight exceeds character budget": "summary_budget",
            "Short notes must not become long reports": "short_note_insights",
            "Analysis contract version mismatch": "contract_version",
            "Missing, duplicate or unknown evidence reference": "evidence_reference",
            "Existing tag requires its catalog identifier": "catalog_identifier",
            "Suggested tag differs from catalog": "catalog_mismatch",
            "New content type is not allowed": "new_content_type",
            "Compact analysis repeats the same statement": "repeated_reading_text",
            "Invalid compact JSON": "invalid_json",
            "Ambiguous compact JSON": "ambiguous_json",
            "Node output exceeds reading budget": "node_reading_budget",
            "Empty or repeated node statement": "node_repeated_statement",
            "Unknown or duplicate node citation": "node_evidence_reference",
            "Node cited originals exceed synthesis budget": "node_citation_budget",
        }
        reason = reasons.get(str(error), "invalid_output")
        fields = []
        validation_types = []
        if isinstance(error, ValidationError):
            allowed = set(AnalysisResult.model_fields) | set(RAGResult.model_fields) | set(DomainSummaryResult.model_fields)
            errors = error.errors(include_input=False, include_context=False, include_url=False)
            fields = sorted({str(item["loc"][0]) for item in errors
                             if item["loc"] and item["loc"][0] in allowed})
            safe_types = {"json_invalid", "json_type", "missing", "extra_forbidden", "string_type",
                          "list_type", "dict_type", "model_type", "int_parsing", "int_type",
                          "literal_error", "value_error", "string_too_long", "string_too_short",
                          "too_long", "too_short", "greater_than_equal", "less_than_equal"}
            validation_types = sorted({item["type"] for item in errors if item["type"] in safe_types})
            if "json_invalid" in validation_types:
                reason = "invalid_json"
            elif "json_type" in validation_types:
                reason = "invalid_json_content_type"
        json_diagnostic = None
        if reason == "invalid_json":
            # Positions and fixed parser categories only; never return model text.
            raw = choice.get("message", {}).get("content")
            if isinstance(raw, str):
                json_diagnostic = {"characters": len(raw), "starts_with_object": raw.lstrip().startswith("{"),
                                   "markdown_fence": raw.strip().startswith("```"), "empty": not raw.strip()}
                try:
                    json.loads(raw)
                except json.JSONDecodeError as decode_error:
                    categories = {"Expecting value": "expected_value", "Extra data": "extra_data",
                                  "Expecting property name enclosed in double quotes": "property_quotes",
                                  "Expecting ',' delimiter": "comma", "Expecting ':' delimiter": "colon",
                                  "Unterminated string starting at": "unterminated_string",
                                  "Invalid control character at": "control_character"}
                    json_diagnostic.update(line=decode_error.lineno, column=decode_error.colno,
                                           category=categories.get(decode_error.msg, "invalid_json"))
                    if decode_error.msg == "Extra data":
                        stripped = raw.lstrip()
                        first, end = json.JSONDecoder().raw_decode(stripped)
                        tail = stripped[end:].lstrip()
                        json_diagnostic["first_fields"] = sorted(set(first) & set(AnalysisResult.model_fields)) if isinstance(first, dict) else []
                        json_diagnostic["tail_start"] = tail[0] if tail and tail[0] in '{}[],:"' else "other"
        budget_diagnostic = None
        if isinstance(snapshot, DomainSummaryRequest) and isinstance(result, DomainSummaryResult):
            points = [result.overview, *result.insights, *result.limitations]
            cited = {c for p in points for c in p.citations}
            originals = {e.id: e for e in snapshot.evidence}
            known = cited & set(originals)
            budget_diagnostic = dict(output_characters=sum(len(p.text) for p in points),
                reading_ceiling=reading_budget(snapshot),
                cited_evidence=len(cited), cited_sources=len({originals[c].source_id for c in known}),
                cited_characters=sum(len(originals[c].text) for c in known),
                citation_character_ceiling=snapshot.max_cited_characters)
        if isinstance(snapshot, DomainSummaryRequest):
            logging.getLogger('uvicorn.error').warning(
                'node_result_rejected request_id=%s prompt_version=%s stage=%s reason=%s fields=%s validation_types=%s budget=%s',
                snapshot.request_id, prompt_version, stage, reason, fields, validation_types, budget_diagnostic)
        raise ModelExecutionError(502, detail={"code": "invalid_provider_result", "stage": stage,
            "reason": reason, "fields": fields, "validation_types": validation_types,
            "budget_diagnostic": budget_diagnostic,
            "json_diagnostic": json_diagnostic,
            "normalization_diagnostic": {"category": error.category, "fields": error.fields} if isinstance(error, CompactJSONError) else None,
            "message": "大模型结果未通过结构或证据校验，未保存为有效分析。"}) from None


def normalize_compact_json(raw: str) -> dict:
    """Normalize complete, disjoint JSON objects without losing or inventing content.

    Providers sometimes emit fields in adjacent objects, or use the overview's
    text/citations shape for summary. Ambiguous or incomplete output still fails.
    The usual contract, budget and evidence checks run after normalization.
    """
    if not isinstance(raw, str):
        raise ValueError("Invalid compact JSON")
    def distinct_fields(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise CompactJSONError("duplicate_field", [key])
            value[key] = item
        return value
    decoder = json.JSONDecoder(object_pairs_hook=distinct_fields)
    result, offset, objects = {}, 0, 0
    allowed = set(AnalysisResult.model_fields)
    while offset < len(raw):
        while offset < len(raw) and raw[offset].isspace():
            offset += 1
        if offset == len(raw):
            break
        value, end = decoder.raw_decode(raw, offset)
        objects += 1
        if objects > len(allowed) or not isinstance(value, dict) or not value:
            raise CompactJSONError("invalid_object")
        if not set(value).issubset(allowed):
            raise CompactJSONError("unexpected_field", set(value) - allowed)
        if set(value) & set(result):
            raise CompactJSONError("duplicate_field", set(value) & set(result))
        result.update(value)
        offset = end
        # A known provider variant closes the outer object before its remaining
        # comma-separated fields. Move only that boundary, retaining every field.
        tail = raw[offset:].lstrip()
        if objects == 1 and tail.startswith(",") and raw.rstrip().endswith("}"):
            combined = json.loads(raw[:end - 1] + raw[end:], object_pairs_hook=distinct_fields)
            if not isinstance(combined, dict) or not set(combined).issubset(allowed):
                raise CompactJSONError("unexpected_field", set(combined) - allowed if isinstance(combined, dict) else ())
            result = combined
            break
    if not result:
        raise ValueError("Invalid compact JSON")
    summary = result.get("summary")
    if isinstance(summary, dict) and set(summary) == {"text", "citations"}:
        if "summary_citations" in result and result["summary_citations"] != summary["citations"]:
            raise CompactJSONError("conflicting_summary_citations", ["summary_citations"])
        result["summary"] = summary["text"]
        result["summary_citations"] = summary["citations"]
    return result
