from dataclasses import replace
from types import SimpleNamespace
import sys

import pytest

from app import files, ocr
from app.database import engine
from sqlalchemy import event
from app.database import SessionLocal
from services.worker import main as worker


@pytest.fixture(autouse=True)
def file_storage(tmp_path, monkeypatch):
    configuration = replace(files.settings, upload_dir=tmp_path / "uploads", max_file_bytes=16)
    monkeypatch.setattr(files, "settings", configuration)
    monkeypatch.setattr(worker, "settings", configuration)
    monkeypatch.setattr(ocr, "settings", configuration, raising=False)
    return configuration.upload_dir


def upload(client, account, body=b"hello\x00world", key="file-1", **params):
    return client.post("/v1/file-captures", params={"domain_id": account["domain"]["id"],
                       "filename": "资料.docx", **params}, content=body,
                       headers={**account["headers"], "Idempotency-Key": key,
                                "Content-Type": "application/octet-stream"})


def test_file_roundtrip_replay_and_delete(client, account, file_storage):
    first = upload(client, account)
    assert first.status_code == 201, first.text
    source = first.json()
    assert source["content_type"] == "file"
    assert source["title"] == "资料.docx"
    assert source["coverage"] == "file_only"
    assert source["processing_status"] == "pending"
    assert source["document_parse"]["status"] == "pending"
    assert source["evidence"] == []
    assert source["file"]["size_bytes"] == 11
    downloaded = client.get(source["file"]["download_path"], headers=account["headers"])
    assert downloaded.content == b"hello\x00world"
    assert "attachment" in downloaded.headers["content-disposition"]
    replay = upload(client, account)
    assert replay.status_code == 200
    assert replay.json()["id"] == source["id"]
    assert len(list(file_storage.iterdir())) == 1
    assert upload(client, account, body=b"different").status_code == 409
    assert len(list(file_storage.iterdir())) == 1
    assert client.delete(f'/v1/sources/{source["id"]}', headers=account["headers"]).status_code == 202
    assert client.get(source["file"]["download_path"], headers=account["headers"]).status_code == 404
    assert upload(client, account).status_code == 409
    with SessionLocal() as db:
        assert worker.run_once(db)
    assert list(file_storage.iterdir()) == []


def test_upload_limits_and_owner_isolation(client, account, file_storage):
    assert upload(client, account, body=b"x" * 17).status_code == 413
    assert list(file_storage.iterdir()) == []
    assert upload(client, account, filename="bad\nname").status_code == 422
    assert upload(client, account, filename="bad.exe").status_code == 422
    assert upload(client, account, filename="fake.pdf").status_code == 422
    assert upload(client, account, domain_id="unknown").status_code == 404
    source = upload(client, account, filename="../../资料.docx").json()
    assert source["file"]["filename"] == "资料.docx"
    other = client.post("/v1/dev/sessions", json={"email": "other@example.com", "name": "Other"}).json()
    headers = {"Authorization": f'Bearer {other["access_token"]}'}
    assert client.get(source["file"]["download_path"], headers=headers).status_code == 404
    assert client.post("/v1/file-captures", params={"domain_id": account["domain"]["id"], "filename": "a"},
                       content=b"a", headers={**headers, "Idempotency-Key": "other"}).status_code == 404
    assert client.get(source["file"]["download_path"]).status_code == 401


def test_desktop_text_and_link(client, account):
    for kind, content, coverage in [("text", {"text": "桌面笔记"}, "full_text"),
                                    ("url", {"text": ""}, "link_only")]:
        payload = {"capture_type": kind, "content": content, "coverage": coverage,
                   "context": {"domain_id": account["domain"]["id"]},
                   "client": {"kind": "desktop", "version": "0.1.0"}}
        if kind == "url":
            payload["source_url"] = "https://example.com/article"
        else:
            payload["title"] = "笔记"
        response = client.post("/v1/captures", json=payload,
                               headers={**account["headers"], "Idempotency-Key": kind})
        assert response.status_code == 201, response.text
        assert response.json()["content_type"] == ("note" if kind == "text" else "link")
        assert response.json()["title"] == ("笔记" if kind == "text" else "https://example.com/article")
    listed = client.get("/v1/sources", headers=account["headers"]).json()["items"]
    assert {item["content_type"] for item in listed} == {"note", "link"}
    assert next(item for item in listed if item["content_type"] == "note")["preview"] == "桌面笔记"
    search = client.post("/v1/search", headers=account["headers"], json={"query": "桌面笔记"}).json()
    assert search["items"][0]["content_type"] == "note"


def test_desktop_project_association_for_text_and_file(client, account):
    headers = account["headers"]
    project_id = account["project"]["id"]
    payload = {"capture_type": "text", "content": {"text": "项目笔记"},
               "coverage": "full_text",
               "context": {"domain_id": account["domain"]["id"], "project_id": project_id},
               "client": {"kind": "desktop", "version": "0.1.0"}}
    text_response = client.post("/v1/captures", json=payload,
                                headers={**headers, "Idempotency-Key": "project-text"})
    assert text_response.status_code == 201
    assert text_response.json()["project_ids"] == [project_id]

    file_response = upload(client, account, key="project-file")
    assert file_response.status_code == 201
    file_id = file_response.json()["id"]
    linked = client.patch(f"/v1/sources/{file_id}/context", headers=headers,
                          json={"project_id": project_id})
    assert linked.status_code == 200
    assert linked.json()["project_ids"] == [project_id]
    sources = client.get("/v1/sources", headers=headers).json()["items"]
    assert {item["id"] for item in sources if project_id in item["project_ids"]} == {
        text_response.json()["id"], file_id}


def test_source_list_pages_and_bounded_queries(client, account):
    headers = account["headers"]
    for index in range(12):
        payload = {"capture_type": "text", "content": {"text": f"第 {index} 条资料"},
                   "coverage": "full_text", "context": {"domain_id": account["domain"]["id"]},
                   "client": {"kind": "desktop", "version": "0.1.0"}}
        response = client.post("/v1/captures", json=payload,
                               headers={**headers, "Idempotency-Key": f"page-{index}"})
        assert response.status_code == 201
    statements = []

    def count_query(*_args):
        statements.append(1)

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        first = client.get("/v1/sources", headers=headers, params={"limit": 5}).json()
    finally:
        event.remove(engine, "before_cursor_execute", count_query)
    assert len(statements) <= 7
    assert len(first["items"]) == 5
    assert first["next_offset"] == 5
    second = client.get("/v1/sources", headers=headers,
                        params={"limit": 5, "offset": first["next_offset"]}).json()
    assert len(second["items"]) == 5
    assert not {item["id"] for item in first["items"]} & {item["id"] for item in second["items"]}
    filtered = client.get("/v1/sources", headers=headers,
                          params={"domain_id": account["domain"]["id"]}).json()
    assert len(filtered["items"]) == 12


def test_scanned_pdf_page_invokes_ocr_once(monkeypatch, tmp_path):
    class FakePage:
        def get_textpage(self):
            return SimpleNamespace(get_text_range=lambda: "", close=lambda: None)

        def get_size(self):
            return (100, 100)

        def render(self, scale):
            return SimpleNamespace(to_pil=lambda: object(), close=lambda: None)

        def close(self):
            pass

    class FakePDF:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            return FakePage()

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=lambda _: FakePDF()))
    calls = []

    def recognize(image, model):
        calls.append(model)
        return "识别结果"

    from services.worker.pdf_ocr import extract_pdf
    assert extract_pdf(tmp_path / "fake.pdf", "small", ocr_image=recognize) == [(1, "识别结果")]
    assert calls == ["small"]


def test_file_supplement_is_partial_coverage(client, account):
    source = upload(client, account).json()
    response = client.post(f'/v1/sources/{source["id"]}/supplements',
                           headers=account["headers"], json={"text": "用户补充的文件摘要"})
    assert response.status_code == 201
    assert response.json()["coverage"] == "partial_text"
    assert response.json()["file"]["filename"] == "资料.docx"


def test_anydoc_document_is_indexed_and_preserves_supplement(client, account):
    headers = account["headers"]
    source = upload(client, account, body=b"docx-stub", key="document-parse").json()
    assert source["ocr"] is None
    assert source["document_parse"]["status"] == "pending"
    assert client.post(f'/v1/sources/{source["id"]}/ocr', headers=headers).status_code == 422
    supplement = client.post(f'/v1/sources/{source["id"]}/supplements', headers=headers,
                             json={"text": "我的备注"})
    assert supplement.status_code == 201
    with SessionLocal() as db:
        assert worker.run_document_once(db, lambda path, filename: "# 项目计划\n\n可搜索的文档内容")
    detail = client.get(f'/v1/sources/{source["id"]}', headers=headers).json()
    assert detail["document_parse"]["status"] == "completed"
    assert detail["processing_status"] == "usable"
    assert detail["extracted_text"] == "# 项目计划\n\n可搜索的文档内容\n\n我的备注"
    assert detail["coverage"] == "partial_text"
    assert len(detail["evidence"]) == 2
    revisions = client.get(f'/v1/sources/{source["id"]}/revisions', headers=headers).json()["items"]
    assert revisions[0]["parser_version"] == "anydoc-v1"
    results = client.post("/v1/search", headers=headers,
                          json={"query": "可搜索的文档内容", "limit": 10}).json()["items"]
    assert any(item["source_id"] == source["id"] for item in results)


def test_anydoc_failure_keeps_original_file(client, account):
    headers = account["headers"]
    source = upload(client, account, body=b"invalid", key="invalid-document").json()
    with SessionLocal() as db:
        def invalid(path, filename):
            raise ValueError("文档解析失败：格式损坏")
        assert worker.run_document_once(db, invalid)
    detail = client.get(f'/v1/sources/{source["id"]}', headers=headers).json()
    assert detail["document_parse"]["status"] == "failed"
    assert detail["document_parse"]["last_error"] == "文档解析失败：格式损坏"
    assert client.get(detail["file"]["download_path"], headers=headers).content == b"invalid"


def test_anydoc_reader_is_local_and_csv_uses_explicit_format(tmp_path, monkeypatch):
    calls = []

    def convert(data, *args):
        calls.append((data, args))
        return "# 表格\n\n内容"

    monkeypatch.setitem(sys.modules, "anydoc", SimpleNamespace(
        to_markdown_bytes=convert, ConvertError=ValueError))
    path = tmp_path / "stored-by-id"
    path.write_bytes(b"a,b\n1,2")
    from services.worker.anydoc_reader import extract_document
    assert extract_document(path, "表格.csv") == "# 表格\n\n内容"
    assert extract_document(path, "报告.docx") == "# 表格\n\n内容"
    assert calls == [(b"a,b\n1,2", ("csv",)), (b"a,b\n1,2", ())]


def test_markdown_upload_preserves_source_and_is_searchable(client, account, monkeypatch):
    monkeypatch.setattr(files, "settings", replace(files.settings, max_file_bytes=1024))
    body = "# 项目计划\r\n\r\n编写代码并测试。".encode("utf-8")
    response = upload(client, account, body=body, filename="计划.MD", key="markdown-file")
    assert response.status_code == 201, response.text
    source = response.json()
    assert source["content_type"] == "file"
    assert source["title"] == "计划.MD"
    assert source["document_parse"]["status"] == "pending"
    with SessionLocal() as db:
        assert worker.run_document_once(db)
    detail = client.get(f'/v1/sources/{source["id"]}', headers=account["headers"]).json()
    assert detail["document_parse"]["status"] == "completed"
    assert detail["extracted_text"] == body.decode("utf-8")
    assert client.get(detail["file"]["download_path"], headers=account["headers"]).content == body
    results = client.post("/v1/search", headers=account["headers"],
                          json={"query": "编写代码", "limit": 10}).json()["items"]
    assert any(item["source_id"] == source["id"] for item in results)


def test_markdown_reader_handles_bom_and_invalid_encoding(tmp_path):
    from services.worker.anydoc_reader import extract_document
    path = tmp_path / "stored-by-id"
    path.write_bytes(b"\xef\xbb\xbf# Header\n\nBody")
    assert extract_document(path, "note.md") == "# Header\n\nBody"
    path.write_bytes(b"    code block\n\nBody  \n")
    assert extract_document(path, "note.md") == "    code block\n\nBody  \n"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match="UTF-8"):
        extract_document(path, "note.md")
    path.write_bytes(b"# Header\x00Body")
    with pytest.raises(ValueError, match="二进制"):
        extract_document(path, "note.md")
    path.write_bytes(b" \n ")
    with pytest.raises(ValueError, match="没有可提取的文字"):
        extract_document(path, "note.md")
    path.write_bytes(b"x" * 200_001)
    with pytest.raises(ValueError, match="200000"):
        extract_document(path, "note.md")


def test_real_anydoc_csv_upload_to_search(client, account):
    pytest.importorskip("anydoc")
    headers = account["headers"]
    source = upload(client, account, body=b"a,b\nAna,NY\n", filename="表格.csv", key="real-csv").json()
    with SessionLocal() as db:
        assert worker.run_document_once(db)
    detail = client.get(f'/v1/sources/{source["id"]}', headers=headers).json()
    assert detail["document_parse"]["status"] == "completed"
    assert "Ana" in detail["extracted_text"]
    results = client.post("/v1/search", headers=headers,
                          json={"query": "Ana", "limit": 10}).json()["items"]
    assert any(item["source_id"] == source["id"] for item in results)


def test_pdf_ocr_settings_search_reprocess_and_history(client, account):
    headers = account["headers"]
    assert client.get("/v1/ocr/preferences", headers=headers).json() == {"model": "small", "engine": "pdfium"}
    assert client.put("/v1/ocr/preferences", headers=headers, json={"model": "large"}).status_code == 422
    assert client.put("/v1/ocr/preferences", headers=headers,
                      json={"model": "small", "engine": "unknown"}).status_code == 422
    assert client.put("/v1/ocr/preferences", headers=headers, json={"model": "tiny"}).json() == {
        "model": "tiny", "engine": "pdfium"}
    source = upload(client, account, body=b"%PDF-stub", filename="资料.pdf").json()
    assert source["processing_status"] == "pending"
    assert source["document_parse"] is None
    assert source["ocr"]["model"] == "tiny"
    assert source["ocr"]["engine"] == "pdfium"
    with SessionLocal() as db:
        assert worker.run_ocr_once(db, lambda path, model: [(1, "第一页的可搜索内容"), (2, "第二页资料")])
    detail = client.get(f'/v1/sources/{source["id"]}', headers=headers).json()
    assert detail["ocr"]["status"] == "completed"
    assert detail["coverage"] == "full_text"
    assert [item["locator"]["start"] for item in detail["evidence"]] == [1, 2]
    found = client.post("/v1/search", headers=headers, json={"query": "可搜索", "limit": 10}).json()["items"]
    assert found[0]["source_id"] == source["id"]
    assert found[0]["locator"]["kind"] == "pdf_page"
    first_evidence = detail["evidence"][0]["evidence_id"]

    assert client.put("/v1/ocr/preferences", headers=headers,
                      json={"model": "medium", "engine": "pdf_inspector"}).status_code == 200
    queued = client.post(f'/v1/sources/{source["id"]}/ocr', headers=headers)
    assert queued.status_code == 202
    assert queued.json()["model"] == "medium"
    assert queued.json()["engine"] == "pdf_inspector"
    assert client.post(f'/v1/sources/{source["id"]}/ocr', headers=headers).status_code == 409
    assert client.put("/v1/ocr/preferences", headers=headers,
                      json={"model": "small", "engine": "pdfium"}).status_code == 200
    with SessionLocal() as db:
        assert worker.run_ocr_once(db, lambda path, model: [(1, "重新识别的内容")])
    detail = client.get(f'/v1/sources/{source["id"]}', headers=headers).json()
    assert detail["revision"] == 2
    assert detail["extracted_text"] == "重新识别的内容"
    history = client.get(f'/v1/evidence/{first_evidence}', headers=headers).json()
    assert history["has_newer_revision"] is True
    revisions = client.get(f'/v1/sources/{source["id"]}/revisions', headers=headers).json()["items"]
    assert revisions[0]["parser_version"] == "pdf-inspector-ppocrv6-medium-v1"
    client.post(f'/v1/sources/{source["id"]}/supplements', headers=headers,
                json={"text": "我的补充说明"})
    assert client.post(f'/v1/sources/{source["id"]}/ocr', headers=headers).status_code == 202
    with SessionLocal() as db:
        assert worker.run_ocr_once(db, lambda path, model: [(1, "第三次识别")])
    detail = client.get(f'/v1/sources/{source["id"]}', headers=headers).json()
    assert detail["extracted_text"] == "第三次识别\n\n我的补充说明"

    other = client.post("/v1/dev/sessions", json={"email": "other-ocr@example.com", "name": "Other"}).json()
    other_headers = {"Authorization": f'Bearer {other["access_token"]}'}
    assert client.get("/v1/ocr/preferences", headers=other_headers).json() == {
        "model": "small", "engine": "pdfium"}
    assert client.post(f'/v1/sources/{source["id"]}/ocr', headers=other_headers).status_code == 404


def test_pdf_engine_is_snapshotted_for_worker(client, account, monkeypatch):
    headers = account["headers"]
    client.put("/v1/ocr/preferences", headers=headers,
               json={"model": "medium", "engine": "pdf_inspector"})
    source = upload(client, account, body=b"%PDF-stub", filename="资料.pdf", key="engine-snapshot").json()
    assert source["ocr"]["engine"] == "pdf_inspector"
    assert client.put("/v1/ocr/preferences", headers=headers, json={"model": "tiny"}).json()[
        "engine"] == "pdf_inspector"
    client.put("/v1/ocr/preferences", headers=headers,
               json={"model": "tiny", "engine": "pdfium"})
    calls = []

    def inspect_pdf(path, model):
        calls.append(model)
        return [(1, "来自 pdf-inspector")]

    monkeypatch.setattr(worker, "extract_pdf_inspector", inspect_pdf)
    monkeypatch.setattr(worker, "extract_pdf", lambda path, model: pytest.fail("选择了错误的 PDF 引擎"))
    with SessionLocal() as db:
        assert worker.run_ocr_once(db)
    assert calls == ["medium"]
    detail = client.get(f'/v1/sources/{source["id"]}', headers=headers).json()
    assert detail["extracted_text"] == "来自 pdf-inspector"
    assert detail["ocr"]["engine"] == "pdf_inspector"


def test_pdf_inspector_uses_markdown_and_ocr_for_scanned_pages(tmp_path, monkeypatch):
    class FakePage:
        def get_size(self):
            return 100, 100

        def render(self, scale):
            assert scale == 2
            return SimpleNamespace(to_pil=lambda: "image", close=lambda: None)

        def close(self):
            pass

    class FakePDF:
        def __len__(self):
            return 2

        def __getitem__(self, index):
            return FakePage()

        def close(self):
            pass

    pages = [SimpleNamespace(page=0, markdown="# 标题", needs_ocr=False),
             SimpleNamespace(page=1, markdown="", needs_ocr=True)]
    monkeypatch.setitem(sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=lambda _: FakePDF()))
    monkeypatch.setitem(sys.modules, "pdf_inspector", SimpleNamespace(
        extract_pages_markdown=lambda _: SimpleNamespace(pages=pages)))
    from services.worker.pdf_ocr import extract_pdf_inspector
    assert extract_pdf_inspector(tmp_path / "fake.pdf", "small", ocr_image=lambda image, model: "扫描正文") == [
        (1, "# 标题"), (2, "扫描正文")]


def test_pdf_ocr_failure_and_retry_preserves_user_supplement(client, account):
    headers = account["headers"]
    source = upload(client, account, body=b"%PDF-stub", filename="资料.pdf").json()
    source_id = source["id"]
    supplement = client.post(f"/v1/sources/{source_id}/supplements", headers=headers,
                             json={"text": "人工补充说明"})
    assert supplement.status_code == 201
    with SessionLocal() as db:
        assert worker.run_ocr_once(db, lambda path, model: [(1, "识别正文")])
    detail = client.get(f"/v1/sources/{source_id}", headers=headers).json()
    assert "人工补充说明" in detail["extracted_text"]
    assert client.post(f"/v1/sources/{source_id}/ocr", headers=headers).status_code == 202
    with SessionLocal() as db:
        def broken(path, model):
            raise ValueError("PDF 已加密")
        assert worker.run_ocr_once(db, broken)
    detail = client.get(f"/v1/sources/{source_id}", headers=headers).json()
    assert detail["ocr"]["status"] == "failed"
    assert detail["revision"] == 2
    assert client.post(f"/v1/sources/{source_id}/ocr", headers=headers).status_code == 202


def test_bundled_ocr_uses_explicit_models_and_rejects_missing(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    from PIL import Image
    from services.worker.pdf_ocr import _ocr_image
    options = {}
    class Engine:
        def __init__(self, **kwargs):
            options.update(kwargs)
        def predict(self, image):
            return [{'res': {'rec_texts': ['本地识别']}}]
    monkeypatch.setitem(sys.modules, 'paddleocr', SimpleNamespace(PaddleOCR=Engine))
    monkeypatch.setenv('PKA_OCR_MODELS_DIR', str(tmp_path))
    monkeypatch.setattr(_ocr_image, 'engines', {}, raising=False)
    for kind in ('det', 'rec'):
        (tmp_path / f'PP-OCRv6_small_{kind}_infer').mkdir()
    image = Image.new('RGB', (8, 8), 'white')
    assert _ocr_image(image, 'small') == '本地识别'
    assert options['text_detection_model_dir'] == str(tmp_path / 'PP-OCRv6_small_det_infer')
    assert options['text_recognition_model_dir'] == str(tmp_path / 'PP-OCRv6_small_rec_infer')
    with pytest.raises(ValueError, match='未包含'):
        _ocr_image(image, 'medium')
