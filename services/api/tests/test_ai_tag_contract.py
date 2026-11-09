"""Versioned AI tagging boundaries, without paid requests."""
import copy
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.ai_contract import AnalysisRequest, AnalysisResult, validate_result
from services.ai_gateway.main import GatewaySettings, create_app


def request_data():
    return {"contract_version": 2, "request_id": str(uuid4()), "kind": "analyze", "source_id": "s", "revision_id": "r",
            "evidence": [{"id": "e", "text": "原始正文", "locator_kind": "paragraph", "locator_start": 0, "locator_end": 1}],
            "tags": [{"id": "t", "name": "人工智能", "dimension": "topic", "aliases": ["AI"]},
                     {"id": "type", "name": "教程", "dimension": "type"}]}


def result_data():
    return {"contract_version": 2, "overview": {"text": "概述", "citations": ["e"]},
            "summary": "摘要", "summary_citations": ["e"], "limitations": [{"text": "限制", "citations": ["e"]}],
            "tag_suggestions": [{"tag_id": "t", "name": "人工智能", "dimension": "topic", "reason": "原文依据", "citations": ["e"]}]}


def test_valid_versions_and_legacy_default():
    validate_result(AnalysisRequest.model_validate(request_data()), AnalysisResult.model_validate(result_data()))
    legacy_request = request_data()
    legacy_request.pop("contract_version")
    legacy = AnalysisResult(summary="摘要", summary_citations=["e"])
    assert validate_result(AnalysisRequest.model_validate(legacy_request), legacy).contract_version == 1


@pytest.mark.parametrize("change", ["version", "overview", "overview_reference", "limitation_reference", "tag_reference", "unknown_id", "name", "dimension", "duplicate_id", "duplicate_name", "empty_name", "new_type", "missing_id", "alias_missing_id", "rejected", "rejected_alias"])
def test_invalid_tag_result_rejected(change):
    snapshot, output = request_data(), result_data()
    tag = output["tag_suggestions"][0]
    if change == "version": output.pop("contract_version")
    elif change == "overview": output["overview"] = None
    elif change == "overview_reference": output["overview"]["citations"] = []
    elif change == "limitation_reference": output["limitations"][0]["citations"] = ["foreign"]
    elif change == "tag_reference": tag["citations"] = ["e", "e"]
    elif change == "unknown_id": tag["tag_id"] = "other-owner"
    elif change == "name": tag["name"] = "其他名称"
    elif change == "dimension": tag["dimension"] = "entity"
    elif change == "duplicate_id": output["tag_suggestions"].append(copy.deepcopy(tag))
    elif change == "duplicate_name":
        output["tag_suggestions"] = [dict(tag, tag_id=None, name=" 新标签 "), dict(tag, tag_id=None, name="新标签")]
    elif change == "empty_name": tag.update(tag_id=None, name="   ")
    elif change == "new_type": tag.update(tag_id=None, name="未知类型", dimension="type")
    elif change == "missing_id": tag["tag_id"] = None
    elif change == "alias_missing_id": tag.update(tag_id=None, name="ＡＩ")
    else: snapshot["rejected_tags"] = [{"name": "人工智能" if change == "rejected" else " ａｉ ", "dimension": "topic"}]
    with pytest.raises(ValueError):
        validate_result(AnalysisRequest.model_validate(snapshot), AnalysisResult.model_validate(output))


def test_new_topic_entity_allowed_and_type_existing():
    output = result_data()
    output["tag_suggestions"] += [dict(output["tag_suggestions"][0], tag_id=None, name="新主题"),
                                   dict(output["tag_suggestions"][0], tag_id=None, name="产品", dimension="entity"),
                                   dict(output["tag_suggestions"][0], tag_id="type", name="教程", dimension="type")]
    validate_result(AnalysisRequest.model_validate(request_data()), AnalysisResult.model_validate(output))


@pytest.mark.parametrize("version", [0, 4, True, "2"])
def test_request_version_is_strict(version):
    with pytest.raises(ValidationError):
        AnalysisRequest.model_validate(dict(request_data(), contract_version=version))


def test_overview_and_candidates_bounded():
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(dict(result_data(), overview={"text": "字" * 301, "citations": ["e"]}))
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(dict(result_data(), tag_suggestions=result_data()["tag_suggestions"] * 9))
    data = request_data()
    data["tags"].append(dict(data["tags"][0], id="other"))
    with pytest.raises(ValidationError):
        AnalysisRequest.model_validate(data)


def test_gateway_v2_prompt_and_version_caching():
    seen = []
    def upstream(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result_data())}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    app = create_app(GatewaySettings("synthetic", "model", "token"), transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        data = request_data()
        first = client.post("/v1/analyze", json=data, headers={"Authorization": "Bearer token"})
        assert first.status_code == 200
        assert first.json()["prompt_version"] == "single-source-tags-v2"
        assert first.json()["result"]["overview"]["text"] == "概述"
        changed = dict(data, contract_version=1)
        assert client.post("/v1/analyze", json=changed, headers={"Authorization": "Bearer token"}).status_code == 409
    assert len(seen) == 1
    prompt = seen[0]["messages"][0]["content"]
    assert "context 只影响标签和归类" in prompt
    assert "rejected_tags" in prompt
