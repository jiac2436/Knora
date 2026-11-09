from __future__ import annotations

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import FeishuDocumentArtifact, SemanticIndexJob, SourceRevision, WebReadJob
from services.worker.main import run_once


def payload(account, **overrides):
    result = {
        "capture_type": "feishu_document", "title": "飞书测试文档",
        "source_url": "https://my.feishu.cn/wiki/ExampleToken#share-selection",
        "content": {"text": "# 飞书测试文档\n\n上下文方案可供 Agent 检索。"},
        "coverage": "full_text", "context": {"domain_id": account["domain"]["id"]},
        "client": {"kind": "desktop", "version": "0.1.0"},
        "feishu_document": {"document_id": "DocToken", "revision_id": 12, "cli_version": "1.0.97"},
    }
    result.update(overrides)
    return result


def save(client, account, body, key="feishu-test"):
    return client.post("/v1/captures", json=body,
                       headers={**account["headers"], "Idempotency-Key": key})


def test_feishu_preserves_markdown_metadata_and_queues_index_not_web(client, account):
    body = payload(account)
    saved = save(client, account, body)
    assert saved.status_code == 201, saved.text
    source = saved.json()
    detail = client.get(f"/v1/sources/{source['id']}", headers=account["headers"]).json()
    assert detail["content_type"] == "link"
    assert detail["processing_status"] == "usable"
    assert detail["extracted_text"] == body["content"]["text"]
    assert detail["web_read"] is None
    assert detail["feishu_document"] == body["feishu_document"]
    assert detail["crawl4ai_markdown_origin"] == "feishu_cli"
    with SessionLocal() as db:
        assert db.scalar(select(WebReadJob).where(WebReadJob.source_id == source["id"])) is None
        assert db.get(SemanticIndexJob, source["id"]).status == "pending"
        revision = db.scalar(select(SourceRevision).where(SourceRevision.source_id == source["id"]))
        assert revision.parser_version == "feishu-cli-v1"
    markdown_path = f"/v1/sources/{source['id']}/crawl4ai-markdown"
    assert client.get(markdown_path, headers=account["headers"]).json()["markdown"] == body["content"]["text"]
    repeated = save(client, account, body)
    assert repeated.status_code == 200
    assert repeated.json()["id"] == source["id"]
    changed = payload(account, feishu_document={"document_id": "DocToken", "revision_id": 13, "cli_version": "1.0.97"})
    assert save(client, account, changed).status_code == 409
    other = client.post("/v1/dev/sessions", json={"email": "other@example.com", "name": "Other"}).json()
    assert client.get(markdown_path, headers={"Authorization": f"Bearer {other['access_token']}"}).status_code == 404
    assert client.post(f"/v1/sources/{source['id']}/web-read", headers=account["headers"]).status_code == 422
    assert client.post(f"/v1/sources/{source['id']}/supplements", json={"text": "我的标注"}, headers=account["headers"]).status_code == 201
    assert client.get(markdown_path, headers=account["headers"]).json()["origin"] == "feishu_cli"
    assert client.delete(f"/v1/sources/{source['id']}", headers=account["headers"]).status_code in {200, 202}
    with SessionLocal() as db:
        assert run_once(db)
        assert not db.scalars(select(FeishuDocumentArtifact)).all()


def test_large_feishu_text_is_not_truncated_and_other_limits_unchanged(client, account):
    text = "正文" * 60_000
    response = save(client, account, payload(account, content={"text": text}))
    assert response.status_code == 201, response.text
    detail = client.get(f"/v1/sources/{response.json()['id']}", headers=account["headers"]).json()
    assert detail["extracted_text"] == text
    ordinary = payload(account, capture_type="text", source_url=None, feishu_document=None, content={"text": text})
    assert save(client, account, ordinary, key="ordinary").status_code == 413
    assert save(client, account, payload(account, content={"text": "中" * 1_000_001}), key="large").status_code == 413


@pytest.mark.parametrize("overrides", [
    {"source_url": "https://feishu.cn.evil.example/wiki/Example"},
    {"source_url": None}, {"feishu_document": None},
    {"client": {"kind": "mobile", "version": "0.1.0"}},
    {"coverage": "link_only"}, {"content": {"text": ""}},
    {"feishu_document": {"document_id": "Doc", "revision_id": -1, "cli_version": "1.0.97"}},
    {"feishu_document": {"document_id": "Doc", "revision_id": 1, "cli_version": "1.0.97", "access_token": "dummy"}},
    {"capture_type": "url"},
])
def test_invalid_feishu_contract_is_rejected(client, account, overrides):
    assert save(client, account, payload(account, **overrides)).status_code == 422
