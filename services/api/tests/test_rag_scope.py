"""Domain/topic questions freeze only current scoped originals, using synthetic data."""
from dataclasses import replace
import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from app import rag, semantic, services
from app.ai import canonical
from app.errors import ApiError
from app.models import Domain, DomainTopic, DomainTopicMember, EvidenceChunk, RAGJob, RAGSession, RAGTurn, Source, SourceRevision
from test_rag_api import storage, create, complete, session, response


@pytest.fixture
def scoped(storage, monkeypatch):
    db, _, source, chunk, owner, other, _ = storage
    monkeypatch.setattr(rag, "search_knowledge", services.search_knowledge)
    monkeypatch.setattr(semantic, "settings", replace(semantic.settings, semantic_enabled=False))
    other_domain = Domain(owner_id=owner.owner_id, name="另一个知识域")
    private_domain = Domain(owner_id=other.owner_id, name="另一个用户的知识域")
    db.add_all([other_domain, private_domain]); db.flush()
    sources = [source]
    for domain, label in [(source.domain_id, "子分类"), (source.domain_id, "另一个主题"), (other_domain.id, "别域")]:
        row = Source(owner_id=owner.owner_id, domain_id=domain, title=label, kind="text", coverage="full_text")
        db.add(row); db.flush()
        revision = SourceRevision(source_id=row.id, version=1, extracted_text="原文事实：本地保存。")
        db.add(revision); db.flush(); row.current_revision_id = revision.id
        db.add(EvidenceChunk(revision_id=revision.id, ordinal=0, text=revision.extracted_text,
            locator_kind="text", locator_start=0, locator_end=len(revision.extracted_text)))
        sources.append(row)
    root = DomainTopic(owner_id=owner.owner_id, domain_id=source.domain_id, scope="app", name="父主题")
    unrelated = DomainTopic(owner_id=owner.owner_id, domain_id=source.domain_id, scope="app", name="其他主题")
    empty = DomainTopic(owner_id=owner.owner_id, domain_id=source.domain_id, scope="app", name="空主题")
    shared = DomainTopic(owner_id=owner.owner_id, domain_id=source.domain_id, scope="shareable", name="共享主题")
    foreign = DomainTopic(owner_id=other.owner_id, domain_id=private_domain.id, scope="app", name="他人主题")
    db.add_all([root, unrelated, empty, shared, foreign]); db.flush()
    child = DomainTopic(owner_id=owner.owner_id, domain_id=source.domain_id, scope="app", name="子主题", parent_id=root.id)
    db.add(child); db.flush()
    for topic, member in [(root, source), (child, source), (child, sources[1]), (unrelated, sources[2]), (root, sources[3])]:
        db.add(DomainTopicMember(topic_id=topic.id, source_id=member.id))
    # An accidental cross-domain membership must never widen retrieval.
    for row in sources:
        services.reindex_source(db, row)
    db.commit()
    return storage, sources, root, child, empty, shared, foreign, other_domain


def test_real_search_domain_and_topic_descendants_are_hard_boundaries(scoped):
    data, sources, root, _, _, _, _, _ = scoped
    body = rag.TurnCreate(session_id=session(data), question="本地保存", confirm_send=True,
        domain_ids=[sources[0].domain_id])
    evidence, _ = rag.freeze_evidence(data[0], data[4].owner_id, body)
    assert {item.source_id for item in evidence} == {item.id for item in sources[:3]}
    body.topic_id = root.id
    evidence, _ = rag.freeze_evidence(data[0], data[4].owner_id, body)
    assert {item.source_id for item in evidence} == {item.id for item in sources[:2]}
    assert len(evidence) == 2  # Repeated parent/child membership is deduplicated.


def test_empty_topic_finishes_locally_without_search_or_paid_job(scoped, monkeypatch):
    data, sources, _, _, empty, _, _, _ = scoped
    monkeypatch.setattr(rag, "search_knowledge", lambda *a, **kw: pytest.fail("Empty topic widened to all sources"))
    result = create(data, domain_ids=[sources[0].domain_id], topic_id=empty.id)
    assert result["status"] == "succeeded" and result["insufficient_evidence"]
    assert result["scope"] == {"domain_ids": [sources[0].domain_id], "topic_id": empty.id}
    assert data[0].scalar(select(RAGJob)) is None
    assert not complete(data, lambda _: pytest.fail("Empty topic invoked provider"))


def test_semantic_recall_cannot_bypass_topic_or_domain_scope(scoped, monkeypatch):
    import numpy as np
    from app.models import EvidenceEmbedding
    data, sources, root, *_ = scoped
    db = data[0]
    monkeypatch.setattr(semantic, "settings", replace(semantic.settings, semantic_enabled=True))
    monkeypatch.setattr(semantic, "embed_query", lambda _: np.array([1.0, 0.0], dtype=np.float32))
    for source in sources:
        chunk = db.scalar(select(EvidenceChunk).where(EvidenceChunk.revision_id == source.current_revision_id))
        db.add(EvidenceEmbedding(owner_id=source.owner_id, source_id=source.id, revision_id=source.current_revision_id,
            evidence_id=chunk.id, start=0, end=len(chunk.text), model=semantic.MODEL_NAME,
            vector=np.array([1.0, 0.0], dtype=np.float32).tobytes()))
    db.commit()
    evidence, _ = rag.freeze_evidence(db, data[4].owner_id,
        rag.TurnCreate(session_id=session(data), question="synonym-query-without-lexical-hit", confirm_send=True,
            domain_ids=[sources[0].domain_id], topic_id=root.id))
    assert {item.source_id for item in evidence} == {source.id for source in sources[:2]}


@pytest.mark.parametrize("invalid", ["no_domain", "wrong_domain", "shared", "other_owner"])
def test_topic_scope_requires_current_owner_domain_and_app_scope(scoped, invalid):
    data, sources, root, _, _, shared, foreign, other_domain = scoped
    domains, topic = [sources[0].domain_id], root.id
    if invalid == "no_domain": domains = None
    elif invalid == "wrong_domain": domains = [other_domain.id]
    elif invalid == "shared": topic = shared.id
    else: topic = foreign.id
    with pytest.raises(ApiError) as error:
        create(data, domain_ids=domains, topic_id=topic)
    assert error.value.status_code == (422 if invalid == "no_domain" else 404)
    assert data[0].scalar(select(RAGJob)) is None


def test_legacy_unscoped_request_still_searches_all_owned_domains(scoped):
    data, sources, *_ = scoped
    evidence, _ = rag.freeze_evidence(data[0], data[4].owner_id,
        rag.TurnCreate(session_id=session(data), question="本地保存", confirm_send=True))
    assert {item.source_id for item in evidence} == {item.id for item in sources}
    key, sid = uuid4(), session(data)
    result = create(data, sid, key, question="not-present-unique")
    old_body = {"session_id": sid, "question": "not-present-unique", "confirm_send": True,
        "domain_ids": None, "project_id": None, "tag_query": None}
    assert data[0].get(RAGTurn, result["id"]).request_hash == hashlib.sha256(canonical(old_body).encode()).hexdigest()


@pytest.mark.parametrize("mutation", ["moved_domain", "excluded"])
@pytest.mark.parametrize("timing", ["before_send", "during_call"])
def test_changed_scope_cancels_frozen_request_before_send_or_publish(scoped, mutation, timing):
    data, sources, root, child, _, _, _, other_domain = scoped
    # Restrict to a single source so a synthetic response can cite its evidence.
    db = data[0]
    db.get(DomainTopicMember, (child.id, sources[1].id)).included = False
    db.commit()
    result = create(data, domain_ids=[sources[0].domain_id], topic_id=root.id, question="本地保存")
    def mutate():
        if mutation == "moved_domain": sources[0].domain_id = other_domain.id
        else:
            db.get(DomainTopicMember, (root.id, sources[0].id)).included = False
            db.get(DomainTopicMember, (child.id, sources[0].id)).included = False
        db.commit()
    calls = []
    def handler(request):
        calls.append(request)
        body = response(data, request)
        mutate()
        import httpx
        return httpx.Response(200, json=body)
    if timing == "before_send": mutate()
    assert complete(data, handler)
    saved = db.get(RAGTurn, result["id"])
    assert saved.status == "cancelled"
    assert json.loads(saved.result_json) == {"scope": result["scope"]}
    assert len(calls) == (0 if timing == "before_send" else 1)


def test_scope_history_survives_completion_and_is_separate_from_other_scope(scoped):
    data, sources, root, *_ = scoped
    sid = session(data)
    first = create(data, sid, domain_ids=[sources[0].domain_id], topic_id=root.id, question="本地保存")
    complete(data)
    saved = data[0].get(RAGTurn, first["id"])
    assert json.loads(saved.result_json)["scope"] == first["scope"]
    plain = create(data, sid, question="not-present-unique")
    scoped_turns = rag.list_turns(sid, 100, data[4], data[0], sources[0].domain_id, root.id)["items"]
    assert [row["id"] for row in scoped_turns] == [first["id"]]
    assert [row["id"] for row in rag.list_turns(sid, 100, data[4], data[0])["items"]] == [plain["id"]]
    assert [row["id"] for row in rag.list_sessions(data[4], data[0], sources[0].domain_id, root.id)["items"]] == [sid]
    sources[0].domain_id = scoped[7].id
    data[0].commit()
    assert all(item["stale"] for item in rag.turn_view(data[0], saved)["citations"])


def test_scope_unknown_preserves_identity_and_never_retries(scoped):
    data, sources, root, *_ = scoped
    key, sid = uuid4(), session(data)
    created = create(data, sid, key, domain_ids=[sources[0].domain_id], topic_id=root.id, question="本地保存")
    import httpx
    assert complete(data, lambda _: httpx.Response(502))
    assert rag.turn_view(data[0], data[0].get(RAGTurn, created["id"]))["scope"] == created["scope"]
    assert create(data, sid, key, domain_ids=[sources[0].domain_id], topic_id=root.id, question="本地保存")["id"] == created["id"]
    assert not complete(data, lambda _: pytest.fail("Unknown scoped request retried"))
    with pytest.raises(ApiError) as error:
        create(data, sid, key, domain_ids=[sources[0].domain_id], question="本地保存")
    assert error.value.code == "rag_request_conflict"


def test_scope_history_filter_precedes_the_page_limit(scoped):
    from datetime import datetime, timezone
    data, sources, root, _, _, _, _, other_domain = scoped
    db = data[0]
    wanted = create(data, question="not-present-unique", domain_ids=[sources[0].domain_id], topic_id=root.id)
    kept = db.get(RAGTurn, wanted["id"])
    old_date = datetime(2020, 1, 1, tzinfo=timezone.utc)
    db.get(RAGSession, wanted["session_id"]).created_at = old_date
    kept.created_at = old_date
    other_scope = {"domain_ids": [other_domain.id], "topic_id": None}
    for _ in range(101):
        record = RAGSession(owner_id=data[4].owner_id, title="别域会话")
        db.add(record); db.flush()
        db.add(RAGTurn(owner_id=data[4].owner_id, session_id=record.id, question="别域问题", status="succeeded",
            request_key=str(uuid4()), request_hash="a" * 64, result_json=canonical({"scope": other_scope})))
        # A mixed session must also apply scope before its question limit.
        db.add(RAGTurn(owner_id=data[4].owner_id, session_id=wanted["session_id"], question="别域问题", status="succeeded",
            request_key=str(uuid4()), request_hash="b" * 64, result_json=canonical({"scope": other_scope})))
    db.commit()
    found = rag.list_sessions(data[4], db, sources[0].domain_id, root.id)["items"]
    assert [item["id"] for item in found] == [wanted["session_id"]]
    found = rag.list_turns(wanted["session_id"], 100, data[4], db, sources[0].domain_id, root.id)["items"]
    assert [item["id"] for item in found] == [wanted["id"]]
