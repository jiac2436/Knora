"""Scoped retrieval combines summaries and labels without substituting model prose for evidence."""
from dataclasses import replace
import json

import httpx
import numpy as np
import pytest
from sqlalchemy import select

from app import ai, knowledge_tags, semantic, services, summary_index
from app.errors import ApiError
from app.models import AIArtifact, Domain, EvidenceChunk, EvidenceEmbedding, PersonalContext, Source, SourceRevision, SummaryEmbedding, TagAssignment
from app.schemas import SearchRequest
from services.worker import ai_analysis as worker
from test_ai_analysis import storage, create, response


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch):
    monkeypatch.setattr(semantic, "settings", replace(semantic.settings, semantic_enabled=False))


def analyzed(data, summary="quasar architecture"):
    db = data[0]
    create(data, kind="analyze")
    body = response(data)
    body["result"].update(summary=summary, insights=[], organizing_suggestion=None,
        overview={"text": summary, "citations": [data[3].id]})
    worker.run_ai_once(db, httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    artifact = db.scalar(select(AIArtifact).order_by(AIArtifact.created_at.desc()))
    summary_index.ensure_summary_index(db)
    while summary_index.index_summary_once(db):
        pass
    services.reindex_source(db, data[2]); db.commit()
    return artifact


def search(data, query, **kwargs):
    return services.search_knowledge(data[0], data[6].owner_id, SearchRequest(query=query, **kwargs))


def test_summary_keyword_returns_original_evidence_and_separate_summary_snippet(storage):
    artifact = analyzed(storage)
    rows = search(storage, "quasar")
    assert len(rows) == 1
    row = rows[0]
    assert row["match_sources"] == ["summary"] and row["match_type"] == "keyword"
    assert row["summary_artifact_id"] == artifact.id and "quasar" in row["summary_snippet"]
    assert row["evidence_id"] == storage[3].id and row["snippet"] == storage[3].text
    assert "quasar" not in row["snippet"]


@pytest.mark.parametrize("change", ["title", "context", "revision", "deleted", "deleting"])
def test_stale_or_deleted_summary_is_never_retrieved(storage, change):
    analyzed(storage)
    db, _, source, _, _, _, _, _ = storage
    if change == "title":
        source.title = "Changed"
    elif change == "context":
        db.add(PersonalContext(source_id=source.id, notes="Edited personal interpretation"))
    elif change == "revision":
        revision = SourceRevision(source_id=source.id, version=2, extracted_text="new")
        db.add(revision); db.flush(); source.current_revision_id = revision.id
    elif change == "deleted":
        source.deleted_at = ai.utcnow()
    else:
        source.processing_status = "deleting"
    db.commit()
    assert search(storage, "quasar") == []


def test_only_latest_valid_summary_is_retrieved_and_segments_are_excluded(storage):
    first = analyzed(storage, "quasar")
    db = storage[0]
    snapshot = json.loads(first.snapshot_json)
    snapshot["phase"] = "segment"
    first.snapshot_json = ai.canonical(snapshot); db.commit()
    assert search(storage, "quasar") == []
    analyzed(storage, "nebula")
    assert not search(storage, "quasar")
    assert len(search(storage, "nebula")) == 1


def test_summary_scope_honors_owner_domain_project_and_agent_visibility(storage):
    analyzed(storage)
    db, _, source, _, _, project, owner, other = storage
    assert services.search_knowledge(db, other.owner_id, SearchRequest(query="quasar")) == []
    assert search(storage, "quasar", project_id=project.id) == []
    domain = Domain(owner_id=owner.owner_id, name="Unrelated"); db.add(domain); db.commit()
    assert search(storage, "quasar", domain_ids=[domain.id]) == []
    source.visibility = "app_only"; db.commit()
    assert services.search_knowledge(db, owner.owner_id, SearchRequest(query="quasar"), {source.domain_id}, True) == []
    assert services.search_knowledge(db, owner.owner_id, SearchRequest(query="quasar"), set(), True) == []


def test_manual_label_and_alias_retrieve_without_original_keyword(storage):
    db, _, source, chunk, _, _, owner, _ = storage
    tag = knowledge_tags.create_tag(knowledge_tags.CreateTag(name="检索增强生成"), owner, db)
    knowledge_tags.add_alias(tag["id"], knowledge_tags.AliasRequest(name="RAG"), owner, db)
    knowledge_tags.assign_tag(source.id, knowledge_tags.AssignmentRequest(tag_id=tag["id"]), owner, db)
    rows = search(storage, "rag")
    assert len(rows) == 1 and rows[0]["match_sources"] == ["tag"]
    assert rows[0]["evidence_id"] == chunk.id
    assert rows[0]["tags"] == [{"id": tag["id"], "name": "检索增强生成", "dimension": "topic", "origins": ["manual"]}]
    assert len(search(storage, "rag", tag_query=" RAG ")) == 1
    assert search(storage, "rag", tag_query="Unknown label") == []


def test_stale_ai_assignment_cannot_filter_or_retrieve_but_manual_origin_can(storage):
    db, _, source, _, _, _, owner, _ = storage
    tag = knowledge_tags.create_tag(knowledge_tags.CreateTag(name="RAG"), owner, db)
    db.add(TagAssignment(source_id=source.id, tag_id=tag["id"], origin="ai", revision_id="older")); db.commit()
    assert search(storage, "rag", tag_ids=[tag["id"]]) == []
    knowledge_tags.assign_tag(source.id, knowledge_tags.AssignmentRequest(tag_id=tag["id"]), owner, db)
    assert search(storage, "rag", tag_ids=[tag["id"]])[0]["tags"][0]["origins"] == ["manual"]


def test_foreign_or_ungranted_tag_id_is_not_observable(storage):
    db, _, source, _, _, _, owner, other = storage
    tag = knowledge_tags.create_tag(knowledge_tags.CreateTag(name="Private label"), owner, db)
    with pytest.raises(ApiError):
        services.search_knowledge(db, other.owner_id, SearchRequest(query="private", tag_ids=[tag["id"]]))
    with pytest.raises(ApiError) as error:
        services.search_knowledge(db, owner.owner_id, SearchRequest(query="private", tag_ids=[tag["id"]]), {source.domain_id}, True)
    assert error.value.status_code == 404
    knowledge_tags.assign_tag(source.id, knowledge_tags.AssignmentRequest(tag_id=tag["id"]), owner, db)
    assert services.search_knowledge(db, owner.owner_id, SearchRequest(query="private", tag_ids=[tag["id"]]), {source.domain_id}, True)
    source.visibility = "app_only"; db.commit()
    with pytest.raises(ApiError):
        services.search_knowledge(db, owner.owner_id, SearchRequest(query="private", tag_ids=[tag["id"]]), {source.domain_id}, True)


def test_summary_slice_reference_resolves_original_current_evidence(storage):
    artifact = analyzed(storage)
    db, _, source, chunk, _, _, _, _ = storage
    snapshot = json.loads(artifact.snapshot_json)
    item = snapshot["evidence"][0]
    item.update(id="virtual-slice", original_evidence_id=chunk.id, text_start=2, text_end=10, text=chunk.text[2:10])
    result = json.loads(artifact.result_json)
    result["summary_citations"] = ["virtual-slice"]
    result["overview"]["citations"] = ["virtual-slice"]
    artifact.snapshot_json, artifact.result_json = ai.canonical(snapshot), ai.canonical(result)
    db.commit()
    rows = search(storage, "quasar")
    assert rows[0]["evidence_id"] == chunk.id
    assert rows[0]["snippet"] == chunk.text[2:]
    assert rows[0]["locator"]["start"] == chunk.locator_start + 2


@pytest.mark.parametrize("invalid", ["foreign", "text", "offset", "missing"])
def test_invalid_summary_reference_is_ignored_even_with_keyword_index(storage, invalid):
    artifact = analyzed(storage)
    db = storage[0]
    snapshot = json.loads(artifact.snapshot_json)
    item = snapshot["evidence"][0]
    if invalid == "foreign":
        source = Source(owner_id=storage[7].owner_id, domain_id=storage[2].domain_id, title="Foreign", kind="text", coverage="full_text")
        db.add(source); db.flush()
        revision = SourceRevision(source_id=source.id, version=1, extracted_text="Synthetic evidence")
        db.add(revision); db.flush()
        foreign = EvidenceChunk(revision_id=revision.id, ordinal=0, text="Synthetic evidence", locator_end=18)
        db.add(foreign); db.flush()
        item["original_evidence_id"] = foreign.id
    elif invalid == "text":
        item["text"] = "Fabricated evidence"
    elif invalid == "offset":
        item.update(text_start=999, text_end=1017)
    else:
        snapshot["evidence"] = []
    artifact.snapshot_json = ai.canonical(snapshot); db.commit()
    assert search(storage, "quasar") == []


def test_summary_and_original_vectors_share_one_query_embedding(storage, monkeypatch):
    artifact = analyzed(storage)
    db, _, source, chunk, _, _, _, _ = storage
    monkeypatch.setattr(semantic, "settings", replace(semantic.settings, semantic_enabled=True, semantic_min_similarity=0.1))
    vector = np.asarray([1.0, 0.0], dtype=np.float32)
    db.add(SummaryEmbedding(artifact_id=artifact.id, owner_id=source.owner_id, source_id=source.id,
        revision_id=source.current_revision_id, ordinal=0, start=0, end=10, model=semantic.MODEL_NAME, vector=vector.tobytes()))
    db.add(EvidenceEmbedding(owner_id=source.owner_id, source_id=source.id, revision_id=source.current_revision_id,
        evidence_id=chunk.id, start=0, end=10, model=semantic.MODEL_NAME, vector=vector.tobytes()))
    db.commit()
    calls = []
    monkeypatch.setattr(semantic, "embed_query", lambda query: calls.append(query) or vector)
    rows = search(storage, "related concept")
    assert calls == ["related concept"]
    assert rows[0]["match_type"] == "semantic" and rows[0]["match_sources"] == ["original", "summary"]


def test_label_filter_applies_before_semantic_limit(storage, monkeypatch):
    analyzed(storage)
    db, _, source, chunk, _, _, owner, _ = storage
    tag = knowledge_tags.create_tag(knowledge_tags.CreateTag(name="Only target"), owner, db)
    knowledge_tags.assign_tag(source.id, knowledge_tags.AssignmentRequest(tag_id=tag["id"]), owner, db)
    monkeypatch.setattr(semantic, "settings", replace(semantic.settings, semantic_enabled=True, semantic_min_similarity=0.1))
    target_vector = np.asarray([0.6, 0.8], dtype=np.float32)
    best = np.asarray([1.0, 0.0], dtype=np.float32)
    for index in range(61):
        unrelated = Source(owner_id=owner.owner_id, domain_id=source.domain_id, title=f"Unrelated {index}", kind="text", coverage="full_text")
        db.add(unrelated); db.flush()
        revision = SourceRevision(source_id=unrelated.id, version=1, extracted_text="unrelated")
        db.add(revision); db.flush(); unrelated.current_revision_id = revision.id
        evidence = EvidenceChunk(revision_id=revision.id, ordinal=0, text="unrelated", locator_end=9)
        db.add(evidence); db.flush()
        db.add(EvidenceEmbedding(owner_id=owner.owner_id, source_id=unrelated.id, revision_id=revision.id,
            evidence_id=evidence.id, start=0, end=9, model=semantic.MODEL_NAME, vector=best.tobytes()))
    db.add(EvidenceEmbedding(owner_id=owner.owner_id, source_id=source.id, revision_id=source.current_revision_id,
        evidence_id=chunk.id, start=0, end=10, model=semantic.MODEL_NAME, vector=target_vector.tobytes()))
    db.commit()
    monkeypatch.setattr(semantic, "embed_query", lambda _: best)
    rows = search(storage, "unmatched", tag_ids=[tag["id"]], limit=1)
    assert rows[0]["source_id"] == source.id


def test_pending_ai_suggestion_does_not_filter_or_retrieve_until_accepted(storage):
    from app.ai_tag_suggestions import accept_suggestion
    from test_ai_tag_suggestions import publish, result
    db, _, source, _, _, _, owner, _ = storage
    tag = knowledge_tags.create_tag(knowledge_tags.CreateTag(name="RAG"), owner, db)
    candidate = publish(storage, result(storage, tag_id=tag["id"]))
    services.reindex_source(db, source); db.commit()
    assert search(storage, "RAG") == []
    assert search(storage, "Synthetic", tag_ids=[tag["id"]]) == []
    accept_suggestion(candidate.id, owner, db)
    assert search(storage, "RAG")[0]["tags"][0]["origins"] == ["ai"]


def test_derived_vector_cannot_borrow_private_artifact_through_authorized_source(storage, monkeypatch):
    artifact = analyzed(storage)
    db, _, source, _, _, _, owner, _ = storage
    other_domain = Domain(owner_id=owner.owner_id, name="Private domain")
    db.add(other_domain); db.flush()
    authorized = Source(owner_id=owner.owner_id, domain_id=other_domain.id, title="Visible", kind="text", coverage="full_text")
    db.add(authorized); db.flush()
    revision = SourceRevision(source_id=authorized.id, version=1, extracted_text="Authorized evidence")
    db.add(revision); db.flush(); authorized.current_revision_id = revision.id
    evidence = EvidenceChunk(revision_id=revision.id, ordinal=0, text=revision.extracted_text, locator_end=19)
    db.add(evidence); db.flush()
    vector = np.asarray([1.0, 0.0], dtype=np.float32)
    # Each foreign key is individually legal, but their associations do not agree.
    db.add(SummaryEmbedding(artifact_id=artifact.id, owner_id=owner.owner_id, source_id=authorized.id,
        revision_id=revision.id, ordinal=0, start=0, end=10, model=semantic.MODEL_NAME, vector=vector.tobytes()))
    db.add(EvidenceEmbedding(owner_id=owner.owner_id, source_id=authorized.id, revision_id=revision.id,
        evidence_id=storage[3].id, start=0, end=10, model=semantic.MODEL_NAME, vector=vector.tobytes()))
    db.commit()
    monkeypatch.setattr(semantic, "settings", replace(semantic.settings, semantic_enabled=True, semantic_min_similarity=0.1))
    monkeypatch.setattr(semantic, "embed_query", lambda _: vector)
    assert services.search_knowledge(db, owner.owner_id, SearchRequest(query="related concept"), {other_domain.id}, True) == []


def test_summary_keyword_supports_ascii_prefix_search(storage):
    analyzed(storage, "Architecture overview")
    assert search(storage, "arch")[0]["match_sources"] == ["summary"]


def test_long_task_context_keeps_label_query_within_sql_expression_budget(storage):
    analyzed(storage)
    context = " ".join(f"concept{i}" for i in range(350))
    assert len(context) < 4000
    assert search(storage, "quasar", task_context=context)[0]["match_sources"] == ["summary"]
