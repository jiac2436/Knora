"""Short questions return scoped candidates, never asserted personal facts."""
from dataclasses import replace
import os

import numpy as np
import pytest

from app import semantic, services
from app.models import EvidenceEmbedding, SourceRevision
from app.schemas import SearchRequest
from test_ai_analysis import storage


def index_note(data, monkeypatch, similarity=0.3576):
    db, _, source, chunk, _, _, _, _ = data
    chunk.text = "我的名字叫杰克，我是一名程序员。"
    chunk.locator_end = len(chunk.text)
    source.title = "笔记"
    db.get(SourceRevision, source.current_revision_id).extracted_text = chunk.text
    services.reindex_source(db, source)
    monkeypatch.setattr(semantic, "settings", replace(semantic.settings, semantic_enabled=True, semantic_min_similarity=0.5))
    vector = np.array([similarity, np.sqrt(1 - similarity ** 2)], dtype=np.float32)
    monkeypatch.setattr(semantic, "embed_query", lambda _: np.array([1, 0], dtype=np.float32))
    db.add(EvidenceEmbedding(owner_id=source.owner_id, source_id=source.id,
        evidence_id=chunk.id, revision_id=source.current_revision_id,
        start=0, end=len(chunk.text), model=semantic.MODEL_NAME, vector=vector.tobytes()))
    db.commit()


def search(data, query, **kwargs):
    return services.search_knowledge(data[0], data[6].owner_id, SearchRequest(query=query, **kwargs))


def test_short_category_can_retrieve_original_as_uncertain_candidate(storage, monkeypatch):
    index_note(storage, monkeypatch)
    rows = search(storage, "职业")
    assert len(rows) == 1
    assert rows[0]["snippet"] == "我的名字叫杰克，我是一名程序员。"
    assert rows[0]["requires_evidence_confirmation"] is True
    assert rows[0]["retrieval_confidence"] == "candidate"
    assert rows[0]["evidence_id"] == storage[3].id
    assert "requires_evidence_confirmation" not in search(storage, "程序员")[0]


@pytest.mark.parametrize("restriction", ["grant", "private", "deleted", "tag"])
def test_fallback_keeps_access_and_filter_boundaries(storage, monkeypatch, restriction):
    index_note(storage, monkeypatch)
    db, _, source, _, _, _, owner, _ = storage
    if restriction == "private":
        source.visibility = "app_only"
    elif restriction == "deleted":
        from app.models import utcnow
        source.deleted_at = utcnow()
    db.commit()
    if restriction == "tag":
        assert search(storage, "职业", tag_query="不存在的标签") == []
    else:
        grants = set() if restriction == "grant" else {source.domain_id}
        assert services.search_knowledge(db, owner.owner_id, SearchRequest(query="职业"), grants, True) == []


def test_unrelated_low_similarity_and_long_questions_do_not_fallback(storage, monkeypatch):
    index_note(storage, monkeypatch, similarity=0.2)
    assert search(storage, "明天天气") == []
    index = storage[0].query(EvidenceEmbedding).first()
    index.vector = np.array([0.4, np.sqrt(0.84)], dtype=np.float32).tobytes()
    storage[0].commit()
    assert search(storage, "long unrelated question without exact words") == []


def test_task_context_does_not_create_unrelated_keyword_hits(storage, monkeypatch):
    monkeypatch.setattr(semantic, "settings", replace(semantic.settings, semantic_enabled=False))
    assert search(storage, "职业", task_context="Synthetic evidence") == []


@pytest.mark.skipif(not os.getenv("PKA_RETRIEVAL_TEST_MODEL"), reason="requires an explicitly supplied offline model")
def test_real_local_model_retrieves_personal_note_and_rejects_weather(storage, monkeypatch):
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name=semantic.MODEL_NAME,
        specific_model_path=os.environ["PKA_RETRIEVAL_TEST_MODEL"], cache_dir="/tmp/pka-retrieval-test-cache")
    index_note(storage, monkeypatch)
    monkeypatch.setattr(semantic, "_model", lambda: model)
    monkeypatch.setattr(semantic, "embed_query", lambda text: np.frombuffer(
        semantic._normalize(next(model.embed([text]))), dtype=np.float32))
    embedding = storage[0].query(EvidenceEmbedding).first()
    embedding.vector = semantic.embed_documents([storage[3].text])[0]
    storage[0].commit()
    for query in ["职业", "我的职业是什么", "程序员", "我的名字", "杰克"]:
        rows = search(storage, query)
        assert rows and rows[0]["evidence_id"] == storage[3].id, query
    for query in ["明天天气", "火星大气层", "蛋糕烘焙温度"]:
        assert search(storage, query) == [], query
