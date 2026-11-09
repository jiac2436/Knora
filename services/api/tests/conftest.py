from __future__ import annotations

import os

os.environ["PKA_DATABASE_URL"] = "sqlite:///:memory:"
os.environ["PKA_ENVIRONMENT"] = "test"

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def account(client: TestClient):
    response = client.post("/v1/dev/sessions", json={"email": "owner@example.com", "name": "Owner"})
    assert response.status_code == 201
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    domain = client.post("/v1/domains", headers=headers, json={"name": "工作"}).json()
    project = client.post("/v1/projects", headers=headers, json={"name": "商品管理页"}).json()
    return {"headers": headers, "domain": domain, "project": project, "user": response.json()["user"]}


def capture_payload(domain_id: str, project_id: str | None = None, **overrides):
    payload = {
        "capture_type": "browser_selection",
        "title": "商品列表性能案例",
        "source_url": "https://example.com/article?utm_source=test&id=7",
        "content": {"text": "虚拟列表可以减少商品列表页面的 DOM 节点。", "image_urls": []},
        "coverage": "partial_text",
        "context": {
            "domain_id": domain_id,
            "project_id": project_id,
            "note": "给商品管理页参考",
            "save_reason": "页面性能优化",
        },
        "client": {"kind": "browser_extension", "version": "0.1.0"},
    }
    payload.update(overrides)
    return payload

