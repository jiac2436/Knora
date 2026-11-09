from __future__ import annotations

import asyncio
import os
import pytest
from sqlalchemy import delete, select

from fastapi.testclient import TestClient

from conftest import capture_payload
from app.database import SessionLocal
from app.models import SourceSearchDocument, SourceSearchTerm, SourceTag, SourceTagState
from app.services import ensure_search_index
from app.semantic import index_one
import numpy as np


def create_capture(client: TestClient, account: dict, key: str = "capture-1") -> dict:
    response = client.post(
        "/v1/captures",
        headers={**account["headers"], "Idempotency-Key": key},
        json=capture_payload(account["domain"]["id"], account["project"]["id"]),
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_agent(client: TestClient, account: dict, domain_ids: list[str] | None = None) -> dict:
    response = client.post(
        "/v1/agent-connections",
        headers=account["headers"],
        json={
            "client_name": "M1 Test Agent",
            "domain_ids": domain_ids or [account["domain"]["id"]],
            "expires_in_days": 30,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_development_session_me_and_lists(client: TestClient, account: dict):
    me = client.get("/v1/me", headers=account["headers"])
    assert me.status_code == 200
    assert me.json() == {"id": account["user"]["id"], "display_name": "Owner"}
    assert client.get("/v1/domains", headers=account["headers"]).json()["items"] == [account["domain"]]
    assert client.get("/v1/projects", headers=account["headers"]).json()["items"] == [account["project"]]


def test_capture_is_idempotent_and_conflicting_payload_is_rejected(client: TestClient, account: dict):
    payload = capture_payload(account["domain"]["id"], account["project"]["id"])
    headers = {**account["headers"], "Idempotency-Key": "stable-key"}
    first = client.post("/v1/captures", headers=headers, json=payload)
    second = client.post("/v1/captures", headers=headers, json=payload)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"
    assert first.json()["id"] == second.json()["id"]
    payload["content"]["text"] = "不同内容"
    conflict = client.post("/v1/captures", headers=headers, json=payload)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"


def test_auto_tags_follow_current_content_and_appear_in_lists(client: TestClient, account: dict):
    payload = capture_payload(
        account["domain"]["id"], title="我的爱好",
        content={"text": "我喜欢写代码，也研究人工智能。", "image_urls": []},
    )
    response = client.post(
        "/v1/captures", headers={**account["headers"], "Idempotency-Key": "tags-1"}, json=payload,
    )
    assert response.status_code == 201, response.text
    source_id = response.json()["id"]
    assert response.json()["tags"] == ["人工智能", "编程"]
    listed = client.get("/v1/sources", headers=account["headers"]).json()["items"]
    assert listed[0]["tags"] == ["人工智能", "编程"]

    updated = client.post(
        f"/v1/sources/{source_id}/supplements", headers=account["headers"],
        json={"text": "周末也会跑步。"},
    )
    assert updated.status_code == 201, updated.text
    assert updated.json()["tags"] == ["人工智能", "编程", "运动"]

    from app.tags import ensure_auto_tags
    with SessionLocal() as db:
        db.execute(delete(SourceTag).where(SourceTag.source_id == source_id))
        db.execute(delete(SourceTagState).where(SourceTagState.source_id == source_id))
        db.commit()
        ensure_auto_tags(db)
    restored = client.get(f"/v1/sources/{source_id}", headers=account["headers"])
    assert restored.json()["tags"] == ["人工智能", "编程", "运动"]


def test_capture_rechecks_domain_and_project_ownership(client: TestClient, account: dict):
    other_session = client.post(
        "/v1/dev/sessions", json={"email": "other@example.com", "name": "Other"}
    ).json()
    other_headers = {"Authorization": f"Bearer {other_session['access_token']}"}
    other_domain = client.post("/v1/domains", headers=other_headers, json={"name": "私有域"}).json()
    response = client.post(
        "/v1/captures",
        headers={**account["headers"], "Idempotency-Key": "cross-account"},
        json=capture_payload(other_domain["id"], account["project"]["id"]),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_revision_is_immutable_and_old_evidence_stays_addressable(client: TestClient, account: dict):
    source = create_capture(client, account)
    old_evidence = source["evidence"][0]
    supplement = client.post(
        f"/v1/sources/{source['id']}/supplements",
        headers=account["headers"],
        json={"text": "实践时需要固定行高。"},
    )
    assert supplement.status_code == 201
    assert supplement.json()["revision"] == 2
    old = client.get(f"/v1/evidence/{old_evidence['evidence_id']}", headers=account["headers"])
    assert old.status_code == 200
    assert old.json()["revision"] == 1
    assert old.json()["text"] == old_evidence["text"]
    assert old.json()["has_newer_revision"] is True
    listing = client.get(f"/v1/sources/{source['id']}/revisions", headers=account["headers"])
    assert listing.status_code == 200
    assert [item["version"] for item in listing.json()["items"]] == [2, 1]
    assert listing.json()["items"][0]["is_current"] is True
    assert listing.json()["items"][1]["evidence"][0]["evidence_id"] == old_evidence["evidence_id"]
    other = client.post("/v1/dev/sessions", json={"email": "revision-other@example.com", "name": "Other"}).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert client.get(f"/v1/sources/{source['id']}/revisions", headers=other_headers).status_code == 404


def test_source_detail_contains_current_text(client: TestClient, account: dict):
    source = create_capture(client, account)
    detail = client.get(f"/v1/sources/{source['id']}", headers=account["headers"])
    assert detail.status_code == 200
    assert detail.json()["extracted_text"] == "虚拟列表可以减少商品列表页面的 DOM 节点。"
    assert detail.json()["context"]["note"] == "给商品管理页参考"


def test_context_version_prevents_silent_overwrite_and_project_can_be_cleared(client: TestClient, account: dict):
    source = create_capture(client, account)
    version = source["context"]["version"]
    updated = client.patch(
        f"/v1/sources/{source['id']}/context",
        headers=account["headers"],
        json={"note": "新的个人判断", "project_ids": [], "version": version},
    )
    assert updated.status_code == 200
    assert updated.json()["project_ids"] == []
    stale = client.patch(
        f"/v1/sources/{source['id']}/context",
        headers=account["headers"],
        json={"note": "过期写入", "version": version},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "context_version_conflict"


def test_source_versions_reports_changes_without_returning_content(client: TestClient, account: dict):
    source = create_capture(client, account)
    source_id = source["id"]
    endpoint = "/v1/sources/versions"
    request = {"source_ids": [source_id]}
    initial = client.post(endpoint, headers=account["headers"], json=request).json()["items"][0]
    assert initial == {"source_id": source_id, "revision": 1,
                       "context_version": source["context"]["version"]}
    assert client.patch(f"/v1/sources/{source_id}/context", headers=account["headers"],
                        json={"note": "更新后的笔记"}).status_code == 200
    note_version = client.post(endpoint, headers=account["headers"], json=request).json()["items"][0]
    assert note_version["revision"] == 1
    assert note_version["context_version"] > initial["context_version"]
    assert client.post(f"/v1/sources/{source_id}/supplements", headers=account["headers"],
                       json={"text": "补充内容"}).status_code == 201
    latest = client.post(endpoint, headers=account["headers"], json=request).json()["items"][0]
    assert latest["revision"] == 2
    assert "text" not in latest and "note" not in latest
    other = client.post("/v1/dev/sessions", json={"email": "other@example.com", "name": "Other"}).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert client.post(endpoint, headers=other_headers, json=request).json()["items"] == []
    assert client.delete(f"/v1/sources/{source_id}", headers=account["headers"]).status_code == 202
    assert client.post(endpoint, headers=account["headers"], json=request).json()["items"] == []


def test_search_includes_context_and_latest_experience(client: TestClient, account: dict):
    source = create_capture(client, account)
    experience = client.post(
        f"/v1/sources/{source['id']}/experiences",
        headers=account["headers"],
        json={
            "result": "partially_effective",
            "notes": "滚动性能改善，但可访问性需补测",
            "project_id": account["project"]["id"],
        },
    )
    assert experience.status_code == 201
    result = client.post("/v1/search", headers=account["headers"], json={"query": "DOM 性能"})
    assert result.status_code == 200
    item = result.json()["items"][0]
    assert item["source_id"] == source["id"]
    assert item["personal_context"]["note"] == "给商品管理页参考"
    assert item["verification"]["result"] == "partially_effective"


def test_search_anchors_nearby_chinese_terms_in_long_article(client: TestClient, account: dict):
    text = "🙂清代八旗分别驻守内城。\n\n" + "北京历史资料。\n" * 300 + \
        "1926年，冯玉祥国民军驻守天津大沽口炮台。\n"
    payload = capture_payload(account["domain"]["id"], content={"text": text, "image_urls": []})
    saved = client.post("/v1/captures", headers={**account["headers"], "Idempotency-Key": "history-search"},
                        json=payload)
    assert saved.status_code == 201, saved.text
    result = client.post("/v1/search", headers=account["headers"], json={"query": "冯玉祥驻守"})
    assert result.status_code == 200
    item = result.json()["items"][0]
    assert "冯玉祥国民军驻守" in item["snippet"]
    assert item["locator"]["kind"] == "text"
    assert text[item["locator"]["start"]:item["locator"]["end"]] == "冯玉祥国民军驻守"


def test_search_index_updates_with_note_and_revision_and_backfills(client: TestClient, account: dict):
    source = create_capture(client, account)
    source_id = source["id"]
    headers = account["headers"]
    assert client.patch(f"/v1/sources/{source_id}/context", headers=headers,
                        json={"note": "专属海鸥笔记"}).status_code == 200
    found = client.post("/v1/search", headers=headers, json={"query": "海鸥"}).json()["items"]
    assert [item["source_id"] for item in found] == [source_id]
    assert client.post(f"/v1/sources/{source_id}/supplements", headers=headers,
                       json={"text": "新增鲸鱼经验"}).status_code == 201
    found = client.post("/v1/search", headers=headers, json={"query": "鲸鱼"}).json()["items"]
    assert [item["source_id"] for item in found] == [source_id]
    with SessionLocal() as db:
        document_id = db.scalar(select(SourceSearchDocument.id).where(SourceSearchDocument.source_id == source_id))
        db.execute(delete(SourceSearchTerm).where(SourceSearchTerm.document_id == document_id))
        db.commit()
        ensure_search_index(db)
    found = client.post("/v1/search", headers=headers, json={"query": "鲸鱼"}).json()["items"]
    assert [item["source_id"] for item in found] == [source_id]
    assert client.delete(f"/v1/sources/{source_id}", headers=headers).status_code == 202
    assert client.post("/v1/search", headers=headers, json={"query": "鲸鱼"}).json()["items"] == []


def test_hybrid_search_semantic_evidence_and_agent_visibility(client: TestClient, account: dict, monkeypatch):
    from app import semantic

    monkeypatch.setattr(semantic, "embed_documents", lambda texts: [
        np.array([1.0, 0.0], dtype=np.float32).tobytes() if "写代码" in text else
        np.array([0.0, 1.0], dtype=np.float32).tobytes() for text in texts
    ])
    monkeypatch.setattr(semantic, "embed_query", lambda query: np.array([1.0, 0.0], dtype=np.float32))
    payload = capture_payload(account["domain"]["id"], title="生活记录",
                              content={"text": "我喜欢写代码。", "image_urls": []})
    saved = client.post("/v1/captures", headers={**account["headers"], "Idempotency-Key": "hobby"},
                        json=payload).json()
    before = client.post("/v1/search", headers=account["headers"], json={"query": "我的爱好"}).json()
    assert before["items"] == []
    with SessionLocal() as db:
        assert index_one(db)
    result = client.post("/v1/search", headers=account["headers"], json={"query": "我的爱好"}).json()
    assert result["items"][0]["source_id"] == saved["id"]
    assert result["items"][0]["match_type"] == "semantic"
    assert "写代码" in result["items"][0]["snippet"]
    assert result["items"][0]["evidence_id"] == saved["evidence"][0]["evidence_id"]
    monkeypatch.setattr(semantic, "embed_query", lambda query: (_ for _ in ()).throw(RuntimeError("model unavailable")))
    fallback = client.post("/v1/search", headers=account["headers"], json={"query": "写代码"}).json()
    assert fallback["items"][0]["match_type"] == "keyword"
    assert fallback["semantic_index"] == "failed"
    monkeypatch.setattr(semantic, "embed_query", lambda query: np.array([1.0, 0.0], dtype=np.float32))
    other_domain = client.post("/v1/domains", headers=account["headers"], json={"name": "其他"}).json()
    scoped = client.post("/v1/search", headers=account["headers"],
                         json={"query": "我的爱好", "domain_ids": [other_domain["id"]]}).json()
    assert scoped["items"] == []
    assert client.patch(f"/v1/sources/{saved['id']}/context", headers=account["headers"],
                        json={"visibility": "app_only"}).status_code == 200
    agent = create_agent(client, account)
    agent_result = client.post("/v1/mcp/tools/search_knowledge",
                               headers={"Authorization": f"Bearer {agent['access_token']}"},
                               json={"query": "我的爱好"}).json()
    assert agent_result["items"] == []
    assert client.delete(f"/v1/sources/{saved['id']}", headers=account["headers"]).status_code == 202
    assert client.post("/v1/search", headers=account["headers"], json={"query": "我的爱好"}).json()["items"] == []


def test_semantic_search_ignores_stale_revision_before_reindex(client: TestClient, account: dict, monkeypatch):
    from app import semantic
    from app.models import Source, SourceRevision, EvidenceChunk
    from app.services import reindex_source

    monkeypatch.setattr(semantic, "embed_documents", lambda texts: [
        np.array([1.0, 0.0], dtype=np.float32).tobytes() for _ in texts
    ])
    monkeypatch.setattr(semantic, "embed_query", lambda query: np.array([1.0, 0.0], dtype=np.float32))
    payload = capture_payload(account["domain"]["id"], title="生活记录",
                              content={"text": "我喜欢写代码。", "image_urls": []})
    source_id = client.post("/v1/captures", headers={**account["headers"], "Idempotency-Key": "stale"},
                            json=payload).json()["id"]
    with SessionLocal() as db:
        assert index_one(db)
        source = db.get(Source, source_id)
        revision = SourceRevision(source_id=source_id, version=2, extracted_text="今天学习烹饪。")
        db.add(revision)
        db.flush()
        db.add(EvidenceChunk(revision_id=revision.id, ordinal=0, text=revision.extracted_text,
                             locator_kind="text", locator_start=0, locator_end=len(revision.extracted_text)))
        source.current_revision_id = revision.id
        reindex_source(db, source)
        db.commit()
    found = client.post("/v1/search", headers=account["headers"], json={"query": "我的爱好"}).json()["items"]
    assert found == []


def test_semantic_index_failure_keeps_keyword_search_available(client: TestClient, account: dict, monkeypatch):
    from app import semantic
    from app.models import SemanticIndexJob

    source = create_capture(client, account)
    monkeypatch.setattr(semantic, "embed_documents", lambda texts: (_ for _ in ()).throw(RuntimeError("offline")))
    with SessionLocal() as db:
        with pytest.raises(RuntimeError):
            index_one(db)
        assert db.get(SemanticIndexJob, source["id"]).status == "failed"
    result = client.post("/v1/search", headers=account["headers"], json={"query": "虚拟列表"}).json()
    assert result["items"][0]["source_id"] == source["id"]
    assert result["semantic_index"] == "failed"


def test_agent_domain_visibility_move_delete_and_revoke_are_immediate(client: TestClient, account: dict):
    source = create_capture(client, account)
    evidence_id = source["evidence"][0]["evidence_id"]
    agent = create_agent(client, account)
    agent_headers = {"Authorization": f"Bearer {agent['access_token']}"}

    found = client.post(
        "/v1/mcp/tools/search_knowledge", headers=agent_headers, json={"query": "虚拟列表"}
    )
    assert found.status_code == 200
    assert [item["source_id"] for item in found.json()["items"]] == [source["id"]]
    assert client.post(
        "/v1/mcp/tools/get_evidence",
        headers=agent_headers,
        json={"evidence_id": evidence_id},
    ).status_code == 200

    app_only = client.patch(
        f"/v1/sources/{source['id']}/context",
        headers=account["headers"],
        json={"visibility": "app_only"},
    )
    assert app_only.status_code == 200
    denied = client.post(
        "/v1/mcp/tools/get_evidence", headers=agent_headers, json={"evidence_id": evidence_id}
    )
    assert denied.status_code == 404

    client.patch(
        f"/v1/sources/{source['id']}/context",
        headers=account["headers"],
        json={"visibility": "app_and_agents"},
    )
    private_domain = client.post("/v1/domains", headers=account["headers"], json={"name": "生活"}).json()
    moved = client.patch(
        f"/v1/sources/{source['id']}/context",
        headers=account["headers"],
        json={"domain_id": private_domain["id"]},
    )
    assert moved.status_code == 200
    assert client.post(
        "/v1/mcp/tools/get_evidence", headers=agent_headers, json={"evidence_id": evidence_id}
    ).status_code == 404

    client.patch(
        f"/v1/sources/{source['id']}/context",
        headers=account["headers"],
        json={"domain_id": account["domain"]["id"]},
    )
    deleted = client.delete(f"/v1/sources/{source['id']}", headers=account["headers"])
    assert deleted.status_code == 202
    operation = client.get(
        f"/v1/operations/{deleted.json()['operation_id']}", headers=account["headers"]
    )
    assert operation.status_code == 200
    assert operation.json()["status"] == "pending"
    assert client.get(f"/v1/sources/{source['id']}", headers=account["headers"]).status_code == 404
    assert client.post(
        "/v1/mcp/tools/get_evidence", headers=agent_headers, json={"evidence_id": evidence_id}
    ).status_code == 404

    from app.database import SessionLocal
    from services.worker.main import run_once

    with SessionLocal() as db:
        assert run_once(db) is True
    completed = client.get(
        f"/v1/operations/{deleted.json()['operation_id']}", headers=account["headers"]
    )
    assert completed.json()["status"] == "completed"

    revoked = client.delete(f"/v1/agent-connections/{agent['id']}", headers=account["headers"])
    assert revoked.status_code == 204
    assert client.post(
        "/v1/mcp/tools/search_knowledge", headers=agent_headers, json={"query": "虚拟列表"}
    ).status_code == 401


def test_agent_filters_can_only_narrow_grants_and_calls_are_audited(client: TestClient, account: dict):
    source = create_capture(client, account)
    other_domain = client.post("/v1/domains", headers=account["headers"], json={"name": "生活"}).json()
    agent = create_agent(client, account)
    agent_headers = {"Authorization": f"Bearer {agent['access_token']}"}
    response = client.post(
        "/v1/mcp/tools/search_knowledge",
        headers=agent_headers,
        json={"query": "虚拟列表", "domain_ids": [other_domain["id"]]},
    )
    assert response.status_code == 200
    assert response.json()["items"] == []
    client.post(
        "/v1/mcp/tools/get_source", headers=agent_headers, json={"source_id": source["id"]}
    )
    audit = client.get("/v1/audit-events", headers=account["headers"])
    assert audit.status_code == 200
    assert {event["tool"] for event in audit.json()["items"]} >= {"search_knowledge", "get_source"}
    assert all("query" not in event for event in audit.json()["items"])


def test_non_browser_capture_rejects_image_urls(client: TestClient, account: dict):
    payload = capture_payload(account["domain"]["id"])
    payload['capture_type'] = 'browser_selection'
    payload["content"]["image_urls"] = ["https://example.com/image.png"]
    response = client.post(
        "/v1/captures",
        headers={**account["headers"], "Idempotency-Key": "image-not-supported"},
        json=payload,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "images_not_supported"


def test_official_mcp_server_exposes_and_executes_read_only_tools(client: TestClient, account: dict):
    create_capture(client, account)
    agent = create_agent(client, account)
    os.environ["PKA_AGENT_TOKEN"] = agent["access_token"]
    from app.mcp_server import mcp

    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == {"search_knowledge", "get_evidence", "get_source", "get_summary", "get_tags", "list_knowledge"}
    result = asyncio.run(mcp.call_tool("search_knowledge", {"query": "虚拟列表"}))
    assert result[0].type == "text"
    assert "商品列表性能案例" in result[0].text


def test_create_source_revision_in_editing_mode(client: TestClient, account: dict):
    source = create_capture(client, account)
    source_id = source["id"]
    new_markdown = "# 修改后的标题\n\n| 表格列1 | 表格列2 |\n|---|---|\n| 数据A | 数据B |\n\n> [!NOTE]\n> 这是通过编辑模式保存的内容"
    res = client.post(
        f"/v1/sources/{source_id}/revisions",
        headers=account["headers"],
        json={"text": new_markdown}
    )
    assert res.status_code == 201
    updated = res.json()
    assert updated["revision"] == 2
    assert updated["reading_text"] == new_markdown

    # 验证搜索索引同步更新
    search_res = client.post(
        "/v1/search",
        headers=account["headers"],
        json={"query": "表格列1"}
    )
    assert search_res.status_code == 200
    assert any(item["source_id"] == source_id for item in search_res.json()["items"])
