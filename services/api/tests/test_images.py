import hashlib
import io
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import SourceImage
from app.schemas import CaptureCreate
from app.services import payload_digest
from services.worker.main import run_once
from test_feishu_capture import payload, save


def png(color="white"):
    output = io.BytesIO()
    Image.new("RGB", (16, 8), color).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture()
def image_storage(monkeypatch, tmp_path):
    local = replace(settings, upload_dir=tmp_path)
    monkeypatch.setattr("app.images.settings", local)
    monkeypatch.setattr("services.worker.main.settings", local)
    return tmp_path


def capture_image(client, account, image_id=None, status="pending", key="image-document", retrieval_method=None):
    image_id = image_id or str(uuid4())
    body = payload(account, feishu_images=[{
        "id": image_id, "ordinal": 0, "block_id": "Block1", "caption": "流程图",
        "status": status, "failure_reason": "下载受限" if status == "unavailable" else "",
        "ocr_text": "流程优化启发",
    }])
    if retrieval_method is not None:
        body["feishu_images"][0]["retrieval_method"] = retrieval_method
    saved = save(client, account, body, key)
    assert saved.status_code == 201, saved.text
    return saved.json(), image_id, body


def test_image_original_ocr_ownership_replay_and_deletion(client, account, image_storage):
    source, image_id, body = capture_image(client, account)
    path = f"/v1/sources/{source['id']}/images/{image_id}"
    assert source["images"][0]["status"] == "pending"
    assert "流程优化启发" in source["extracted_text"]
    assert source["reading_text"] == body["content"]["text"].strip()
    assert "OCR 识别文字" not in source["reading_text"]
    found = client.post("/v1/search", json={"query": "流程优化启发"}, headers=account["headers"]).json()["items"]
    assert any(item["source_id"] == source["id"] for item in found)
    markdown = client.get(f"/v1/sources/{source['id']}/crawl4ai-markdown", headers=account["headers"]).json()
    assert markdown["markdown"] == body["content"]["text"]
    assert client.get(path, headers=account["headers"]).status_code == 404
    original = png()
    uploaded = client.put(path, content=original, headers=account["headers"])
    assert uploaded.status_code == 201, uploaded.text
    downloaded = client.get(path, headers=account["headers"])
    assert downloaded.content == original
    assert downloaded.headers["content-type"] == "image/png"
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert client.put(path, content=original, headers=account["headers"]).status_code == 200
    assert client.put(path, content=png("black"), headers=account["headers"]).status_code == 409
    assert len(list((image_storage / "images").iterdir())) == 1
    detail = client.get(f"/v1/sources/{source['id']}", headers=account["headers"]).json()
    assert detail["images"][0]["width"] == 16
    assert detail["images"][0]["height"] == 8
    assert detail["images"][0]["download_path"] == path
    other = client.post("/v1/dev/sessions", json={"email": "other@example.com", "name": "Other"}).json()
    headers = {"Authorization": f"Bearer {other['access_token']}"}
    assert client.get(path, headers=headers).status_code == 404
    assert client.put(path, content=original, headers=headers).status_code == 404
    assert client.get(path).status_code == 401
    assert client.delete(f"/v1/sources/{source['id']}", headers=account["headers"]).status_code == 202
    assert client.get(path, headers=account["headers"]).status_code == 404
    with SessionLocal() as db:
        assert run_once(db)
        assert not db.scalars(select(SourceImage)).all()
    assert not list((image_storage / "images").iterdir())


def test_image_invalid_bytes_limits_and_unavailable_placeholder(client, account, image_storage, monkeypatch):
    source, image_id, _ = capture_image(client, account, status="unavailable")
    path = f"/v1/sources/{source['id']}/images/{image_id}"
    assert source["images"][0]["failure_reason"] == "下载受限"
    assert source["images"][0]["download_path"] is None
    for invalid in (b"<svg><script>alert(1)</script></svg>", b"not a png", b""):
        assert client.put(path, content=invalid, headers=account["headers"]).status_code == 422
    assert not list((image_storage / "images").iterdir())
    monkeypatch.setattr("app.images.settings", replace(settings, upload_dir=image_storage, max_image_bytes=10))
    assert client.put(path, content=png(), headers=account["headers"]).status_code == 413
    assert not list((image_storage / "images").iterdir())
    monkeypatch.setattr("app.images.settings", replace(settings, upload_dir=image_storage))
    assert client.put(path, content=png(), headers=account["headers"]).status_code == 201
    detail = client.get(f"/v1/sources/{source['id']}", headers=account["headers"]).json()
    assert detail["images"][0]["failure_reason"] == ""


def test_manifest_duplicate_and_cross_source_image_rejected(client, account, image_storage):
    source, image_id, body = capture_image(client, account)
    duplicate = {**body, "feishu_images": body["feishu_images"] * 2}
    assert save(client, account, duplicate, "duplicate").status_code == 422
    assert save(client, account, body, "conflicting-document").status_code == 409
    other_source, _, _ = capture_image(client, account, key="another-document")
    assert client.put(f"/v1/sources/{other_source['id']}/images/{image_id}", content=png(),
                      headers=account["headers"]).status_code == 404


def test_old_capture_digest_remains_compatible(account):
    capture = CaptureCreate.model_validate(payload(account))
    old_body = capture.model_dump(mode="json", exclude_none=True)
    old_body.pop("feishu_images")
    old_digest = hashlib.sha256(json.dumps(old_body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert payload_digest(capture) == old_digest


def test_preview_source_survives_upload_and_detail(client, account, image_storage):
    source, image_id, body = capture_image(client, account, retrieval_method="preview")
    assert source["images"][0]["retrieval_method"] == "preview"
    path = f"/v1/sources/{source['id']}/images/{image_id}"
    preview = png()
    assert client.put(path, content=preview, headers=account["headers"]).status_code == 201
    assert client.get(path, headers=account["headers"]).content == preview
    detail = client.get(f"/v1/sources/{source['id']}", headers=account["headers"]).json()
    assert detail["images"][0]["retrieval_method"] == "preview"
    assert detail["images"][0]["status"] == "saved"
    assert save(client, account, body, "image-document").status_code == 200
    invalid = {**body, "feishu_images": [{**body["feishu_images"][0], "retrieval_method": "unknown"}]}
    assert save(client, account, invalid, "invalid-method").status_code == 422


def test_pre_preview_digest_remains_compatible(client, account, image_storage):
    source, _, body = capture_image(client, account)
    assert source["images"][0]["retrieval_method"] == "download"
    capture = CaptureCreate.model_validate(body)
    old_body = capture.model_dump(mode="json", exclude_none=True)
    for image in old_body["feishu_images"]:
        image.pop("retrieval_method")
    expected = hashlib.sha256(json.dumps(old_body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert payload_digest(capture) == expected


def test_preview_schema_migration_preserves_existing_rows(monkeypatch):
    from sqlalchemy import text
    from app import database
    local = database.make_engine("sqlite:///:memory:")
    try:
        database.Base.metadata.create_all(local)
        with local.begin() as connection:
            connection.execute(text("ALTER TABLE source_images DROP COLUMN retrieval_method"))
            connection.execute(text("INSERT INTO source_images (id, source_id, revision_id, ordinal, block_id, caption, status, failure_reason, ocr_text) VALUES ('old', 'source', 'revision', 0, 'block', 'caption', 'saved', '', 'ocr')"))
        monkeypatch.setattr(database, "engine", local)
        database.create_schema()
        database.create_schema()
        with local.connect() as connection:
            row = connection.execute(text("SELECT retrieval_method, status, ocr_text FROM source_images WHERE id='old'")).one()
            assert tuple(row) == ("download", "saved", "ocr")
    finally:
        local.dispose()


def test_reading_text_hides_only_generated_ocr_and_preserves_supplements(client, account, image_storage):
    image_id = str(uuid4())
    original = "🙂原文中讨论 OCR 识别文字：\n这行属于原文章，应当保留。"
    body = payload(account, content={"text": original}, feishu_images=[{
        "id": image_id, "ordinal": 0, "block_id": "Block", "ocr_text": "独有图片检索关键字",
    }])
    saved = save(client, account, body, "reading-document").json()
    assert saved["reading_text"] == original
    assert "独有图片检索关键字" in saved["extracted_text"]
    assert client.post(f"/v1/sources/{saved['id']}/supplements", json={"text": "个人补充🙂"}, headers=account["headers"]).status_code == 201
    detail = client.get(f"/v1/sources/{saved['id']}", headers=account["headers"]).json()
    assert detail["reading_text"] == original + "\n\n个人补充🙂"
    assert "独有图片检索关键字" in detail["extracted_text"]
    results = client.post("/v1/search", json={"query": "独有图片检索关键字"}, headers=account["headers"]).json()["items"]
    assert any(row["source_id"] == saved["id"] for row in results)
