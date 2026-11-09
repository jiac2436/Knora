"""Long-document phase bounds and source-reference integrity."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ai_contract import AnalysisEvidence, AnalysisRequest, AnalysisResult, validate_result
from services.ai_gateway.main import GatewaySettings, create_app
from test_ai_tag_contract import request_data, result_data


def section_data():
    return {"section_id": 1, "overview": {"text": "分段概述", "citations": ["e"]}, "summary": "分段总结", "summary_citations": ["e"],
            "insights": [{"text": "分段要点", "citations": ["e"]}], "limitations": []}


def synthesis_data():
    return dict(request_data(), phase="synthesis", section_summaries=[section_data()])


@pytest.mark.parametrize("change", ["v1", "empty", "foreign", "missing", "duplicate", "unused", "single_sections", "segment_sections", "too_many"])
def test_phase_snapshot_rejects_invalid_inputs(change):
    data = synthesis_data()
    if change == "v1": data["contract_version"] = 1
    elif change == "empty": data["section_summaries"] = []
    elif change == "foreign": data["section_summaries"][0]["summary_citations"] = ["other"]
    elif change == "missing": data["section_summaries"][0]["overview"]["citations"] = []
    elif change == "duplicate": data["section_summaries"][0]["insights"][0]["citations"] = ["e", "e"]
    elif change == "unused": data["evidence"].append(dict(data["evidence"][0], id="unused"))
    elif change == "single_sections": data["phase"] = "single"
    elif change == "segment_sections": data["phase"] = "segment"
    else: data["section_summaries"] *= 9
    with pytest.raises(ValueError):
        AnalysisRequest.model_validate(data)


def test_segment_disallows_tags_and_organization():
    request = AnalysisRequest.model_validate(dict(request_data(), phase="segment"))
    output = result_data()
    with pytest.raises(ValueError):
        validate_result(request, AnalysisResult.model_validate(output))
    output["tag_suggestions"] = []
    validate_result(request, AnalysisResult.model_validate(output))
    output["organizing_suggestion"] = {"domain_id": None, "project_ids": [], "reason": "建议", "citations": ["e"]}
    with pytest.raises(ValueError):
        validate_result(request, AnalysisResult.model_validate(output))


@pytest.mark.parametrize("phase", ["segment", "synthesis"])
def test_gateway_phase_prompts_return_original_evidence(phase):
    output, seen = result_data(), []
    if phase == "segment": output["tag_suggestions"] = []
    def provider(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(output)}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
    app = create_app(GatewaySettings("synthetic", "model", "token"), transport=httpx.MockTransport(provider))
    data = synthesis_data() if phase == "synthesis" else dict(request_data(), phase="segment")
    with TestClient(app) as client:
        result = client.post("/v1/analyze", json=data, headers={"Authorization": "Bearer token"})
        assert result.status_code == 200
        assert result.json()["prompt_version"].endswith("-" + phase)
        assert result.json()["result"]["summary_citations"] == ["e"]
    prompt = seen[0]["messages"][0]["content"]
    assert "phase=" + phase in prompt
    if phase == "synthesis": assert "原始 evidence id" in prompt


@pytest.mark.parametrize("name", ["a\x00b", "a\u200bb", "ﷺ" * 120])
def test_tag_names_match_local_storage_rules(name):
    data = result_data()
    data["tag_suggestions"][0].update(tag_id=None, name=name)
    with pytest.raises(ValueError):
        AnalysisResult.model_validate(data)


def test_rejected_normalized_name_can_expand_to_160_characters():
    data = request_data()
    data["rejected_tags"] = [{"name": "s" * 160, "dimension": "topic"}]
    AnalysisRequest.model_validate(data)


@pytest.mark.parametrize("change", ["summary", "insight_text", "insight_count", "limitations", "citations", "distinct"])
def test_segment_output_enforces_synthesis_budget(change):
    request = dict(request_data(), phase="segment")
    output = result_data()
    output["tag_suggestions"] = []
    if change == "summary": output["summary"] = "字" * 2001
    elif change == "insight_text": output["insights"] = [{"text": "字" * 401, "citations": ["e"]}]
    elif change == "insight_count": output["insights"] = [{"text": "要点", "citations": ["e"]}] * 6
    elif change == "limitations": output["limitations"] *= 4
    elif change == "citations":
        request["evidence"] = [dict(request["evidence"][0], id=str(i)) for i in range(6)]
        output["summary_citations"] = [str(i) for i in range(6)]
    else:
        ids = [str(i) for i in range(11)]
        request["evidence"] = [dict(request["evidence"][0], id=i) for i in ids]
        output["overview"]["citations"] = ids[:5]
        output["summary_citations"] = ids[5:10]
        output["limitations"] = [{"text": "限制", "citations": ids[10:]}]
    with pytest.raises(ValueError):
        validate_result(AnalysisRequest.model_validate(request), AnalysisResult.model_validate(output))


def test_synthesis_section_identifiers_unique():
    request = synthesis_data()
    request["section_summaries"] *= 2
    with pytest.raises(ValueError):
        AnalysisRequest.model_validate(request)
    request["section_summaries"][1] = dict(section_data(), section_id=2)
    AnalysisRequest.model_validate(request)



def test_evidence_slice_mapping_and_legacy_compatibility():
    original = request_data()["evidence"][0]
    legacy = AnalysisEvidence.model_validate(original)
    assert legacy.original_evidence_id is None and legacy.text_start == 0 and legacy.text_end is None
    sliced = dict(original, id="slice", original_evidence_id="original", text_start=10, text_end=14)
    mapped = AnalysisEvidence.model_validate(sliced)
    assert mapped.original_evidence_id == "original"
    assert mapped.text_end - mapped.text_start == len(mapped.text)
    assert mapped.locator_start == legacy.locator_start
    assert AnalysisEvidence.model_validate_json(mapped.model_dump_json()) == mapped


@pytest.mark.parametrize("offsets", [
    {"text_start": -1, "text_end": 3},
    {"text_start": 10, "text_end": 9},
    {"text_start": 10, "text_end": 15},
    {"text_start": 10, "text_end": 10},
    {"text_start": True, "text_end": 5},
])
def test_evidence_slice_rejects_invalid_text_ranges(offsets):
    with pytest.raises(ValueError):
        AnalysisEvidence.model_validate(dict(request_data()["evidence"][0], **offsets))


def test_multiple_slices_share_original_but_have_unique_wire_ids():
    original = request_data()["evidence"][0]
    data = request_data()
    data["evidence"] = [dict(original, id="slice-1", original_evidence_id="original", text_start=0, text_end=4),
                        dict(original, id="slice-2", original_evidence_id="original", text_start=4, text_end=8)]
    parsed = AnalysisRequest.model_validate(data)
    output = result_data()
    output["overview"]["citations"] = ["slice-1"]
    output["summary_citations"] = ["slice-1", "slice-2"]
    output["limitations"] = []
    output["tag_suggestions"] = []
    validate_result(parsed, AnalysisResult.model_validate(output))
    output["summary_citations"] = ["original"]
    with pytest.raises(ValueError):
        validate_result(parsed, AnalysisResult.model_validate(output))
