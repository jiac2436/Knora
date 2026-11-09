"""Agent knowledge reads use synthetic local artifacts and current grants only."""
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.ai import context_state
from app.database import SessionLocal
from app.models import AIArtifact, EvidenceChunk, Source, TagAssignment, TagSuggestion, utcnow
from test_m1_api import create_agent, create_capture


@pytest.fixture
def knowledge(client, account):
    source = create_capture(client, account)
    agent = create_agent(client, account)
    return source, agent, {"Authorization": f"Bearer {agent['access_token']}"}


def read(client, knowledge, tool):
    source, _, headers = knowledge
    return client.post(f"/v1/mcp/tools/{tool}", headers=headers, json={"source_id": source["id"]})


def add_summary(source_id, *, sliced=False):
    with SessionLocal() as db:
        source = db.get(Source, source_id)
        evidence = db.scalar(select(EvidenceChunk).where(EvidenceChunk.revision_id == source.current_revision_id))
        reference = str(uuid4()) if sliced else evidence.id
        entry = {"id": reference, "text": evidence.text, "locator_kind": evidence.locator_kind,
                 "locator_start": evidence.locator_start, "locator_end": evidence.locator_end}
        if sliced:
            entry.update(original_evidence_id=evidence.id, text_start=2, text_end=10, text=evidence.text[2:10])
        snapshot = {"request_id": str(uuid4()), "contract_version": 2, "phase": "single", "kind": "analyze",
                    "source_id": source.id, "revision_id": source.current_revision_id,
                    "context": context_state(db, source), "evidence": [entry],
                    "domains": [{"id": str(uuid4()), "name": "未授权候选领域仅用于模型分析"}]}
        result = {"contract_version": 2, "summary": "合成总结：虚拟列表减少页面节点。",
                  "summary_citations": [reference], "overview": {"text": "页面性能优化", "citations": [reference]}}
        artifact = AIArtifact(owner_id=source.owner_id, source_id=source.id, revision_id=source.current_revision_id,
                              job_id=str(uuid4()), result_json=json.dumps(result), snapshot_json=json.dumps(snapshot),
                              provider="synthetic", model="synthetic", prompt_version="test", usage_json="{}")
        db.add(artifact); db.commit()
        return artifact.id, evidence.id, reference


def test_missing_summary_is_available_false_and_audited(client, account, knowledge):
    response = read(client, knowledge, "get_summary")
    assert response.status_code == 200, response.text
    assert response.json()["available"] is False
    assert response.json()["unavailable_reason"] == "not_analyzed"
    assert response.json()["next_action"] == "read_original_evidence"
    audit = client.get("/v1/audit-events", headers=account["headers"]).json()["items"]
    assert any(event["tool"] == "get_summary" and event["result"] == "success" for event in audit)


@pytest.mark.parametrize("sliced", [False, True])
def test_summary_reader_resolves_original_evidence_without_private_snapshot(client, account, knowledge, sliced):
    source = knowledge[0]
    artifact_id, evidence_id, reference = add_summary(source["id"], sliced=sliced)
    response = read(client, knowledge, "get_summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True and body["artifact_id"] == artifact_id
    assert body["summary"] == "合成总结：虚拟列表减少页面节点。"
    expected = {"evidence_id": evidence_id, "text_start": 2 if sliced else 0, "text_end": 10 if sliced else None}
    assert body["summary_citations"] == [expected]
    assert body["overview"]["citations"] == [expected]
    assert body["source_coverage"] == "partial_text"
    assert not {"snapshot", "snapshot_json", "context", "usage", "provider"}.intersection(body)
    assert "未授权候选领域仅用于模型分析" not in response.text
    assert "给商品管理页参考" not in response.text
    if sliced: assert reference not in response.text
    evidence = client.post("/v1/mcp/tools/get_evidence", headers=knowledge[2], json={"evidence_id": evidence_id})
    assert evidence.status_code == 200
    audit = client.get("/v1/audit-events", headers=account["headers"]).json()["items"]
    assert any(event["tool"] == "get_summary" and event["source_ids"] == [source["id"]] for event in audit)


def test_tags_reader_only_returns_formal_current_assignments_and_preserves_origins(client, account, knowledge):
    source_id = knowledge[0]["id"]
    tags = {}
    for name in ["双来源标签", "过期智能标签", "当前智能标签", "待确认标签"]:
        response = client.post("/v1/tags", headers=account["headers"], json={"name": name})
        assert response.status_code == 200, response.text
        tags[name] = response.json()["id"]
    artifact_id, evidence_id, _ = add_summary(source_id)
    with SessionLocal() as db:
        source = db.get(Source, source_id)
        for name, origin, revision in [
            ("双来源标签", "manual", "older"), ("双来源标签", "ai", source.current_revision_id),
            ("过期智能标签", "ai", "older"), ("当前智能标签", "ai", source.current_revision_id),
        ]:
            db.add(TagAssignment(source_id=source_id, tag_id=tags[name], origin=origin, revision_id=revision))
        db.add(TagSuggestion(owner_id=source.owner_id, source_id=source_id, artifact_id=artifact_id,
                             revision_id=source.current_revision_id, tag_id=tags["待确认标签"], name="待确认标签",
                             normalized_name="待确认标签", dimension="topic", reason="合成理由",
                             evidence_json=json.dumps([evidence_id]), status="pending"))
        db.commit()
    response = read(client, knowledge, "get_tags")
    assert response.status_code == 200, response.text
    actual = {item["name"]: item for item in response.json()["items"]}
    assert set(actual) == {"双来源标签", "当前智能标签"}
    assert actual["双来源标签"]["origins"] == ["ai", "manual"]
    assert actual["当前智能标签"]["origins"] == ["ai"]
    assert all(item["dimension"] == "topic" for item in actual.values())


def test_tags_reader_empty_is_success_without_owner_catalog(client, knowledge):
    response = read(client, knowledge, "get_tags")
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


@pytest.mark.parametrize("tool", ["get_source", "get_evidence"])
def test_existing_read_tools_also_hide_sources_being_deleted(client, knowledge, tool):
    source, _, headers = knowledge
    with SessionLocal() as db:
        db.get(Source, source["id"]).processing_status = "deleting"
        db.commit()
    payload = ({"source_id": source["id"]} if tool == "get_source" else
               {"evidence_id": source["evidence"][0]["evidence_id"]})
    response = client.post(f"/v1/mcp/tools/{tool}", headers=headers, json=payload)
    assert response.status_code == 404


@pytest.mark.parametrize("tool", ["get_summary", "get_tags"])
def test_unknown_source_is_hidden_and_audited_without_source_ids(client, account, knowledge, tool):
    response = client.post(f"/v1/mcp/tools/{tool}", headers=knowledge[2], json={"source_id": str(uuid4())})
    assert response.status_code == 404
    audit = client.get("/v1/audit-events", headers=account["headers"]).json()["items"]
    denied = [event for event in audit if event["tool"] == tool]
    assert len(denied) == 1 and denied[0]["result"] == "not_found" and denied[0]["source_ids"] == []


@pytest.mark.parametrize("tool", ["get_summary", "get_tags"])
@pytest.mark.parametrize("mutation", ["domain", "app_only", "deleted", "deleting", "foreign"])
def test_agent_reads_hide_unavailable_or_ungranted_sources(client, account, knowledge, tool, mutation):
    source, _, _ = knowledge
    if mutation == "domain":
        private = client.post("/v1/domains", headers=account["headers"], json={"name": "私密领域"}).json()
        client.patch(f"/v1/sources/{source['id']}/context", headers=account["headers"], json={"domain_id": private["id"]})
    elif mutation == "app_only":
        client.patch(f"/v1/sources/{source['id']}/context", headers=account["headers"], json={"visibility": "app_only"})
    else:
        with SessionLocal() as db:
            row = db.get(Source, source["id"])
            if mutation == "deleted": row.deleted_at = utcnow()
            elif mutation == "deleting": row.processing_status = "deleting"
            else:
                session = client.post("/v1/dev/sessions", json={"email": "foreign-agent@example.test", "name": "Foreign"})
                row.owner_id = session.json()["user"]["id"]
            db.commit()
    assert read(client, knowledge, tool).status_code == 404


@pytest.mark.parametrize("tool", ["get_summary", "get_tags"])
def test_agent_reads_revoke_immediately_and_reject_app_token(client, account, knowledge, tool):
    source, agent, _ = knowledge
    app_read = client.post(f"/v1/mcp/tools/{tool}", headers=account["headers"], json={"source_id": source["id"]})
    assert app_read.status_code == 401
    assert client.delete(f"/v1/agent-connections/{agent['id']}", headers=account["headers"]).status_code == 204
    assert read(client, knowledge, tool).status_code == 401


@pytest.mark.parametrize("mutation", ["context", "revision", "segment", "invalid_evidence"])
def test_summary_reader_excludes_stale_segments_and_invalid_evidence(client, knowledge, mutation):
    source_id = knowledge[0]["id"]
    artifact_id, _, _ = add_summary(source_id)
    with SessionLocal() as db:
        source = db.get(Source, source_id)
        artifact = db.get(AIArtifact, artifact_id)
        if mutation == "context": source.title = "已修改标题"
        elif mutation == "revision": artifact.revision_id = str(uuid4())
        else:
            snapshot = json.loads(artifact.snapshot_json)
            if mutation == "segment": snapshot["phase"] = "segment"
            else: snapshot["evidence"][0]["text"] = "伪造的证据文本"
            artifact.snapshot_json = json.dumps(snapshot)
        db.commit()
    response = read(client, knowledge, "get_summary")
    assert response.status_code == 200, response.text
    assert response.json()["available"] is False
    assert response.json()["unavailable_reason"] == "no_current_valid_summary"


def test_list_knowledge_paginated_and_scoped(client, account, knowledge):
    source, agent, headers = knowledge
    from conftest import capture_payload
    second = client.post("/v1/captures", headers={**account["headers"], "Idempotency-Key": "second-note"},
        json=capture_payload(account["domain"]["id"], content={"text": "第二份合成资料", "image_urls": []})).json()
    response = client.post("/v1/mcp/tools/list_knowledge", headers=headers, json={"limit": 1})
    assert response.status_code == 200, response.text
    first_page = response.json()
    assert first_page["truncated"] and first_page["next_offset"] == 1
    assert first_page["connection_id"] == agent["id"]
    next_page = client.post("/v1/mcp/tools/list_knowledge", headers=headers,
                           json={"limit": 1, "offset": 1}).json()
    assert next_page["next_offset"] is None
    assert {first_page["items"][0]["source_id"], next_page["items"][0]["source_id"]} == {source["id"], second["id"]}
    assert first_page["items"][0]["evidence_ids"]
    with SessionLocal() as db:
        db.get(Source, second["id"]).visibility = "app_only"
        db.commit()
    assert len(client.post("/v1/mcp/tools/list_knowledge", headers=headers, json={}).json()["items"]) == 1


@pytest.mark.parametrize("payload", [{"limit": 50}, {"offset": -1}, {"limit": 0}])
def test_list_knowledge_invalid_parameters(client, account, knowledge, payload):
    assert client.post("/v1/mcp/tools/list_knowledge", headers=knowledge[2], json=payload).status_code == 422


@pytest.mark.parametrize("status", ["pending", "running", "failed", "unknown", "completed"])
def test_missing_summary_reports_analysis_state_without_private_errors(client, knowledge, status):
    from app.models import AIJob
    source_id = knowledge[0]["id"]
    with SessionLocal() as db:
        source = db.get(Source, source_id)
        db.add(AIJob(owner_id=source.owner_id, source_id=source_id, kind="analyze", status=status,
                     request_key=str(uuid4()), request_sha256="0" * 64, snapshot_json="{}",
                     last_error="private provider diagnostic"))
        db.commit()
    response = read(client, knowledge, "get_summary")
    expected = "no_current_valid_summary" if status == "completed" else "analysis_" + status
    assert response.json()["unavailable_reason"] == expected
    assert "private provider diagnostic" not in response.text


def test_list_knowledge_denies_revoked_connection_and_ungranted_domain(client, account, knowledge):
    source, agent, headers = knowledge
    private = client.post("/v1/domains", headers=account["headers"], json={"name": "未授权"}).json()
    client.patch(f"/v1/sources/{source['id']}/context", headers=account["headers"], json={"domain_id": private["id"]})
    assert client.post("/v1/mcp/tools/list_knowledge", headers=headers, json={}).json()["items"] == []
    client.delete(f"/v1/agent-connections/{agent['id']}", headers=account["headers"])
    assert client.post("/v1/mcp/tools/list_knowledge", headers=headers, json={}).status_code == 401
