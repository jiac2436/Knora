"""Explicit RAG API/worker lifecycle with synthetic evidence and mocked HTTP."""
from dataclasses import replace
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from app import ai, rag
from app.auth import AppPrincipal
from app.database import Base, make_engine
from app.errors import ApiError
from app.models import Domain, EvidenceChunk, PersonalContext, RAGJob, RAGSession, RAGTurn, Source, SourceRevision, User, utcnow
from services.worker import rag_answer as worker


@pytest.fixture
def storage(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'rag-test.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    configured = replace(ai.settings, ai_gateway_url="http://127.0.0.1:8765", ai_gateway_token="synthetic-secret")
    for module in (ai, rag, worker):
        monkeypatch.setattr(module, "settings", configured)
    with sessions() as db:
        owner, other = User(email="rag@example.test", name="RAG"), User(email="other@example.test", name="Other")
        db.add_all([owner, other]); db.flush()
        domain = Domain(owner_id=owner.id, name="合成领域")
        db.add(domain); db.flush()
        source = Source(owner_id=owner.id, domain_id=domain.id, title="Synthetic", kind="text", coverage="full_text")
        db.add(source); db.flush()
        revision = SourceRevision(source_id=source.id, version=1, extracted_text="原文事实：本地保存。")
        db.add(revision); db.flush(); source.current_revision_id = revision.id
        chunk = EvidenceChunk(revision_id=revision.id, ordinal=0, text=revision.extracted_text,
                              locator_kind="text", locator_start=0, locator_end=len(revision.extracted_text))
        db.add_all([chunk, PersonalContext(source_id=source.id, notes="PRIVATE-PERSONAL-CONTEXT")]); db.commit()
        hits = [{"source_id": source.id, "evidence_id": chunk.id, "snippet": chunk.text}]
        monkeypatch.setattr(rag, "search_knowledge", lambda *_: hits)
        yield db, sessions, source, chunk, AppPrincipal(owner.id), AppPrincipal(other.id), hits
    engine.dispose()


def session(data):
    return rag.create_session(rag.SessionCreate(), data[4], data[0])["id"]


def test_erasing_source_erases_derived_answers_and_paid_snapshots(storage):
    db = storage[0]
    create(storage)
    complete(storage)
    rag.purge_source_turns(db, storage[2].id)
    db.commit()
    assert db.scalar(select(RAGTurn)) is None
    assert db.scalar(select(RAGJob)) is None
    assert db.scalar(select(RAGSession)) is not None


def create(data, session_id=None, key=None, **changes):
    body = {"session_id": session_id or session(data), "question": "资料如何保存？", "confirm_send": True, **changes}
    return rag.create_turn(rag.TurnCreate(**body), str(key or uuid4()), data[4], data[0])


def response(data, request=None):
    job = data[0].scalar(select(RAGJob))
    request_id = json.loads(request.content)["request_id"] if request else job.id
    return {"request_id": request_id, "result": {"answer": "资料在本地保存。", "citations": [data[3].id],
        "insufficient_evidence": False, "warnings": []}, "provider": "synthetic", "model": "synthetic-model",
        "prompt_version": "test-v1", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def complete(data, handler=None):
    return worker.run_rag_once(data[0], httpx.MockTransport(handler or (lambda request: httpx.Response(200, json=response(data, request)))))


def test_session_owner_and_delete_isolation(storage):
    db, _, _, _, principal, other, _ = storage
    identifier = session(storage)
    assert len(rag.list_sessions(principal, db)["items"]) == 1
    assert rag.list_sessions(other, db) == {"items": []}
    for action in (lambda: rag.list_turns(identifier, 100, other, db),
                   lambda: rag.delete_session(identifier, other, db),
                   lambda: rag.create_turn(rag.TurnCreate(session_id=identifier, question="问题", confirm_send=True), str(uuid4()), other, db)):
        with pytest.raises(ApiError) as error:
            action()
        assert error.value.status_code == 404
    assert rag.delete_session(identifier, principal, db) == {"deleted": True}


@pytest.mark.parametrize("invalid", ["confirmation", "blank", "uuid"])
def test_explicit_confirmation_question_and_key(storage, invalid):
    sid = session(storage)
    with pytest.raises(ApiError) as error:
        create(storage, session_id=sid, key="invalid" if invalid == "uuid" else None,
               confirm_send=invalid != "confirmation", question=" " if invalid == "blank" else "问题")
    assert error.value.status_code == (409 if invalid == "confirmation" else 422)
    assert storage[0].scalar(select(RAGTurn)) is None


def test_request_idempotency_and_session_busy(storage):
    sid, key = session(storage), uuid4()
    first = create(storage, sid, key)
    assert create(storage, sid, key)["id"] == first["id"]
    with pytest.raises(ApiError) as error:
        create(storage, sid, key, question="不同问题")
    assert error.value.code == "rag_request_conflict"
    with pytest.raises(ApiError) as error:
        create(storage, sid)
    assert error.value.code == "rag_session_busy"
    assert len(list(storage[0].scalars(select(RAGJob)))) == 1


def test_no_recall_answers_locally_without_gateway(storage, monkeypatch):
    storage[6].clear()
    monkeypatch.setattr(rag, "gateway_configured", lambda: False)
    turn = create(storage)
    assert turn["status"] == "succeeded" and turn["insufficient_evidence"] is True
    assert turn["citations"] == []
    assert storage[0].scalar(select(RAGJob)) is None
    assert not complete(storage, lambda _: pytest.fail("Gateway must not be called"))


def test_original_only_snapshot_success_usage_and_citations(storage):
    turn = create(storage)
    calls = []
    def handler(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert request.url.path == "/v1/answer"
        assert request.headers["authorization"] == "Bearer synthetic-secret"
        assert set(payload) == {"request_id", "question", "evidence"}
        assert payload["evidence"][0]["text"] == storage[3].text
        assert "PRIVATE-PERSONAL-CONTEXT" not in request.content.decode()
        assert len(payload["evidence"]) <= 12
        assert sum(len(item["text"]) for item in payload["evidence"]) <= 18000
        return httpx.Response(200, json=response(storage, request))
    assert complete(storage, handler)
    db = storage[0]
    saved = db.get(RAGTurn, turn["id"])
    assert saved.status == "succeeded"
    assert json.loads(saved.result_json)["usage"] == {"prompt_tokens": 10, "completion_tokens": 5}
    view = rag.turn_view(db, saved)
    assert view["citations"][0]["evidence_id"] == storage[3].id
    assert view["citations"][0]["stale"] is False
    assert not complete(storage, handler) and len(calls) == 1


def test_legacy_answer_displays_source_numbers_without_rewriting_history(storage):
    created = create(storage)
    assert complete(storage)
    db, _, source, chunk, *_ = storage
    turn = db.get(RAGTurn, created['id'])
    raw = json.loads(turn.result_json)
    raw['answer'] = f'保存在本地。[{chunk.id}][{chunk.id.upper()}] 示例 a[0]、[2026-10-17]。'
    turn.result_json = json.dumps(raw)
    db.commit()
    original = turn.result_json
    view = rag.turn_view(db, turn)
    assert view['answer'] == '保存在本地。[1][1] 示例 a[0]、[2026-10-17]。'
    assert view['citations'][0]['evidence_id'] == chunk.id
    assert turn.result_json == original
    source.deleted_at = utcnow(); db.commit()
    stale = rag.turn_view(db, turn)
    assert stale['answer'] == view['answer'] and stale['citations'][0]['stale']
    assert turn.result_json == original


def test_display_numbers_follow_returned_sources_and_preserve_unrelated_identifiers():
    first, second, unrelated = (str(uuid4()) for _ in range(3))
    answer = f'两个来源[{second}][{first}]，业务记录 {unrelated}，普通数组 [1, 2]。'
    assert rag.display_answer(answer, [{'evidence_id': first}, {'evidence_id': second}]) == (
        f'两个来源[2][1]，业务记录 {unrelated}，普通数组 [1, 2]。')
    assert rag.display_answer(f'正文[{unrelated}]', [{'evidence_id': first}]) == f'正文[{unrelated}]'
    assert rag.display_answer(None, []) is None
    source, revision = (str(uuid4()) for _ in range(2))
    assert rag.display_answer(f'证据 {first}，资料 {source}，版本 [{revision}]。',
        [{'evidence_id': first, 'source_id': source, 'revision_id': revision}]) == '证据 [1]，资料 [1]，版本 [1]。'


def test_frozen_evidence_budget_and_dedup(storage):
    db, _, source, _, principal, _, hits = storage
    hits.clear()
    for index in range(10):
        chunk = EvidenceChunk(revision_id=source.current_revision_id, ordinal=index + 1, text="x" * 5000,
                              locator_kind="text", locator_start=index * 5000, locator_end=(index + 1) * 5000)
        db.add(chunk); db.flush()
        hits.append({"source_id": source.id, "evidence_id": chunk.id, "snippet": "x" * 30})
    hits.insert(1, hits[0]); db.commit()
    evidence, metadata = rag.freeze_evidence(db, principal.owner_id,
        rag.TurnCreate(session_id=session(storage), question="问题", confirm_send=True))
    assert len(evidence) == 6 and len(metadata) == 6
    assert all(len(item.text) == 3000 for item in evidence)
    assert sum(len(item.text) for item in evidence) == 18000
    assert len({item.id for item in evidence}) == 6


@pytest.mark.parametrize("failure", ["502", "timeout", "citation", "request_id"])
def test_unknown_or_invalid_response_never_replayed(storage, failure):
    created = create(storage)
    calls = []
    def handler(request):
        calls.append(request)
        if failure == "502":
            return httpx.Response(502, text="synthetic-secret PRIVATE")
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic-secret PRIVATE", request=request)
        body = response(storage, request)
        if failure == "citation":
            body["result"]["citations"] = [str(uuid4())]
        else:
            body["request_id"] = str(uuid4())
        return httpx.Response(200, json=body)
    assert complete(storage, handler)
    turn = storage[0].get(RAGTurn, created["id"])
    assert turn.status == ("unknown" if failure in {"502", "timeout"} else "failed")
    assert json.loads(turn.result_json) == {}
    assert "synthetic-secret" not in turn.last_error and "PRIVATE" not in turn.last_error
    assert not complete(storage, handler) and len(calls) == 1


def mutate_source(data, second, kind):
    source = second.get(Source, data[2].id)
    if kind == "delete":
        source.deleted_at = utcnow()
    else:
        revision = SourceRevision(source_id=source.id, version=2, extracted_text="新原文")
        second.add(revision); second.flush(); source.current_revision_id = revision.id
    second.commit()


@pytest.mark.parametrize("kind", ["edit", "delete"])
def test_changed_before_send_cancels_without_gateway(storage, kind):
    created = create(storage)
    with storage[1]() as second:
        mutate_source(storage, second, kind)
    storage[0].expire_all()
    assert complete(storage, lambda _: pytest.fail("Stale source must not be sent"))
    assert storage[0].get(RAGTurn, created["id"]).status == "cancelled"


@pytest.mark.parametrize("kind", ["edit", "delete", "session_delete"])
def test_late_result_never_published_after_change(storage, kind):
    created = create(storage)
    def handler(request):
        body = response(storage, request)
        with storage[1]() as second:
            if kind == "session_delete":
                rag.delete_session(created["session_id"], storage[4], second)
            else:
                mutate_source(storage, second, kind)
        return httpx.Response(200, json=body)
    assert complete(storage, handler)
    storage[0].expire_all()
    turn = storage[0].get(RAGTurn, created["id"])
    if kind == "session_delete":
        assert turn is None and storage[0].scalar(select(RAGJob)) is None
    else:
        assert turn.status == "cancelled" and json.loads(turn.result_json) == {}


def test_interrupted_and_restored_tasks_are_unknown_without_replay(storage):
    created = create(storage)
    db = storage[0]
    job = db.scalar(select(RAGJob)); job.status = "running"
    turn = db.get(RAGTurn, created["id"]); turn.status = "running"; db.commit()
    worker.interrupt_rag(db); db.commit(); db.expire_all()
    assert db.get(RAGTurn, created["id"]).status == "unknown"
    assert db.get(RAGJob, job.id).status == "unknown"
    assert not complete(storage, lambda _: pytest.fail("Interrupted job replayed"))
    db.execute(delete(RAGJob)); turn = db.get(RAGTurn, created["id"]); turn.status = "pending"; db.commit()
    assert rag.turn_view(db, turn)["status"] == "unknown"
    assert not complete(storage, lambda _: pytest.fail("Restored job replayed"))


def test_http_turn_header_status_and_local_session_flow(storage):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.auth import require_app_principal
    from app.database import get_db
    from app.errors import api_error_handler

    app = FastAPI()
    app.include_router(rag.router)
    app.add_exception_handler(ApiError, api_error_handler)
    app.dependency_overrides[require_app_principal] = lambda: storage[4]
    def database():
        with storage[1]() as db:
            yield db
    app.dependency_overrides[get_db] = database
    storage[6].clear()
    with TestClient(app) as http:
        assert http.get("/v1/rag/config").json() == {"configured": True}
        created = http.post("/v1/rag/sessions", json={"title": "合成问答"})
        assert created.status_code == 201
        sid = created.json()["id"]
        body = {"session_id": sid, "question": "未命中的问题", "confirm_send": True}
        assert http.post("/v1/rag/turns", json=body).status_code == 422
        headers = {"Idempotency-Key": str(uuid4())}
        first = http.post("/v1/rag/turns", json=body, headers=headers)
        assert first.status_code == 202
        assert first.json()["insufficient_evidence"] is True
        assert http.post("/v1/rag/turns", json=body, headers=headers).json()["id"] == first.json()["id"]
        assert http.post("/v1/rag/turns", json={**body, "question": "不同问题"}, headers=headers).status_code == 409
        assert http.get("/v1/rag/turns", params={"session_id": sid}).json()["items"][0]["id"] == first.json()["id"]
        assert http.delete("/v1/rag/sessions/" + sid).json() == {"deleted": True}
        assert http.get("/v1/rag/turns", params={"session_id": sid}).status_code == 404
