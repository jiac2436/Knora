"""Local summary index lifecycle; no gateway or paid models are involved."""
from dataclasses import replace
from datetime import timedelta
import json
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from app import semantic, summary_index as index
from app.ai import context_state
from app.database import Base, make_engine
from app.models import AIArtifact, Domain, Source, SourceRevision, SummaryEmbedding, SummaryIndexJob, SummarySearchTerm, User, utcnow


@pytest.fixture
def storage(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'summary.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(semantic, "settings", replace(semantic.settings, semantic_enabled=False))
    with sessions() as db:
        owner = User(email="summary@example.test", name="Summary")
        other = User(email="other-summary@example.test", name="Other")
        db.add_all([owner, other]); db.flush()
        domain = Domain(owner_id=owner.id, name="领域")
        db.add(domain); db.flush()
        source = Source(owner_id=owner.id, domain_id=domain.id, title="原资料", kind="text", coverage="full_text", processing_status="usable")
        db.add(source); db.flush()
        revision = SourceRevision(source_id=source.id, version=1, extracted_text="原文")
        db.add(revision); db.flush()
        source.current_revision_id = revision.id
        db.commit()
        yield db, sessions, source, other
    engine.dispose()


def artifact(db, source, phase="single", summary="本地知识检索总结", age=0):
    snapshot = {"request_id": str(uuid4()), "contract_version": 2, "phase": phase,
        "kind": "analyze", "source_id": source.id, "revision_id": source.current_revision_id,
        "context": context_state(db, source), "evidence": [{"id": "e", "text": "原文", "locator_kind": "paragraph", "locator_start": 0, "locator_end": 1}]}
    result = {"contract_version": 2, "overview": {"text": "检索概述", "citations": ["e"]},
        "summary": summary, "summary_citations": ["e"]}
    if phase == "synthesis":
        snapshot["analysis_request"] = {**snapshot, "section_summaries": [{"section_id": 1,
            "overview": result["overview"], "summary": "分段总结", "summary_citations": ["e"]}]}
    row = AIArtifact(owner_id=source.owner_id, source_id=source.id, revision_id=source.current_revision_id,
        job_id=str(uuid4()), result_json=json.dumps(result), snapshot_json=json.dumps(snapshot),
        provider="synthetic", model="synthetic", prompt_version="test", usage_json="{}", created_at=utcnow() + timedelta(seconds=age))
    db.add(row); db.commit()
    return row


def test_keyword_index_without_semantic_or_gateway(storage, monkeypatch):
    db, _, source, _ = storage
    row = artifact(db, source)
    monkeypatch.setattr(semantic, "embed_documents", lambda _: pytest.fail("Disabled local model must not run"))
    index.ensure_summary_index(db)
    assert index.summary_index_status(db, source.owner_id) == "pending"
    assert index.index_summary_once(db)
    assert index.summary_index_status(db, source.owner_id) == "ready"
    terms = set(db.scalars(select(SummarySearchTerm.term)))
    assert "知识" in terms and "检索" in terms
    assert not list(db.scalars(select(SummaryEmbedding)))
    assert not index.index_summary_once(db)
    assert index.current_summary_artifact(db, source).id == row.id


def test_latest_valid_full_summary_not_segment_and_stale_exclusion(storage):
    db, _, source, _ = storage
    old = artifact(db, source)
    final = artifact(db, source, phase="synthesis", age=2)
    artifact(db, source, phase="segment", age=3)
    assert index.current_summary_artifact(db, source).id == final.id
    final.result_json = "{}"; db.commit()
    assert index.current_summary_artifact(db, source).id == old.id
    source.title = "新标题"; db.commit()
    assert index.current_summary_artifact(db, source) is None
    index.ensure_summary_index(db)
    assert db.get(SummaryIndexJob, source.id) is None


def test_missing_data_requeues_and_rebuild_owner_scope(storage):
    db, _, source, other = storage
    artifact(db, source)
    index.ensure_summary_index(db); index.index_summary_once(db)
    db.execute(delete(SummarySearchTerm).where(SummarySearchTerm.term == "知识")); db.commit()
    index.ensure_summary_index(db)
    assert db.get(SummaryIndexJob, source.id).status == "pending"
    index.index_summary_once(db)
    assert index.rebuild_summary_index(db, other.id)["queued"] == 0
    assert db.get(SummaryIndexJob, source.id).status == "completed"
    assert index.rebuild_summary_index(db, source.owner_id)["queued"] == 1


def test_local_embedding_failure_preserves_keywords_and_manual_rebuild(storage, monkeypatch):
    db, _, source, _ = storage
    artifact(db, source)
    monkeypatch.setattr(semantic, "settings", replace(semantic.settings, semantic_enabled=True))
    def fail(_): raise RuntimeError("private-model-diagnostic")
    monkeypatch.setattr(semantic, "embed_documents", fail)
    index.ensure_summary_index(db)
    with pytest.raises(RuntimeError): index.index_summary_once(db)
    assert "知识" in set(db.scalars(select(SummarySearchTerm.term)))
    assert db.get(SummaryIndexJob, source.id).last_error == "RuntimeError"
    assert index.summary_index_status(db, source.owner_id) == "failed"
    index.ensure_summary_index(db)
    assert db.get(SummaryIndexJob, source.id).status == "failed"
    monkeypatch.setattr(semantic, "embed_documents", lambda texts: [b"synthetic-vector"] * len(texts))
    index.rebuild_summary_index(db, source.owner_id)
    index.index_summary_once(db)
    assert index.summary_index_status(db, source.owner_id) == "ready"
    embeddings = list(db.scalars(select(SummaryEmbedding)))
    assert embeddings and embeddings[0].model == semantic.MODEL_NAME
    db.delete(embeddings[0]); db.commit()
    index.ensure_summary_index(db)
    assert db.get(SummaryIndexJob, source.id).status == "pending"


@pytest.mark.parametrize("mutation", ["delete", "context", "new_artifact"])
def test_inference_race_cannot_publish_old_summary(storage, monkeypatch, mutation):
    db, sessions, source, _ = storage
    old = artifact(db, source)
    monkeypatch.setattr(semantic, "settings", replace(semantic.settings, semantic_enabled=True))
    def infer(texts):
        with sessions() as other_db:
            current = other_db.get(Source, source.id)
            if mutation == "delete": current.deleted_at = utcnow(); other_db.commit()
            elif mutation == "context": current.title = "编辑后"; other_db.commit()
            else: artifact(other_db, current, summary="新总结", age=3)
        return [b"synthetic"] * len(texts)
    monkeypatch.setattr(semantic, "embed_documents", infer)
    index.ensure_summary_index(db); index.index_summary_once(db)
    assert not list(db.scalars(select(SummaryEmbedding).where(SummaryEmbedding.artifact_id == old.id)))
    assert not list(db.scalars(select(SummarySearchTerm).where(SummarySearchTerm.artifact_id == old.id)))
    job = db.get(SummaryIndexJob, source.id)
    if mutation == "new_artifact": assert job.status == "pending" and job.artifact_id != old.id
    else: assert job is None


def test_legacy_summary_and_revision_change(storage):
    db, _, source, _ = storage
    row = artifact(db, source)
    snapshot = json.loads(row.snapshot_json)
    snapshot.pop("contract_version"); snapshot.pop("phase")
    row.snapshot_json = json.dumps(snapshot)
    row.result_json = json.dumps({"summary": "旧版摘要", "summary_citations": ["e"]})
    db.commit()
    index.ensure_summary_index(db); index.index_summary_once(db)
    assert "旧版摘要" == index.summary_text(row)
    revision = SourceRevision(source_id=source.id, version=2, extracted_text="新版正文")
    db.add(revision); db.flush()
    source.current_revision_id = revision.id; db.commit()
    index.ensure_summary_index(db)
    assert index.current_summary_artifact(db, source) is None
    assert not list(db.scalars(select(SummarySearchTerm)))


def test_other_owner_artifact_never_becomes_current_summary(storage):
    db, _, source, other = storage
    valid = artifact(db, source)
    foreign = artifact(db, source, age=3)
    foreign.owner_id = other.id; db.commit()
    assert index.current_summary_artifact(db, source).id == valid.id
    index.ensure_summary_index(db); index.index_summary_once(db)
    assert {r.owner_id for r in db.scalars(select(SummarySearchTerm))} == {source.owner_id}
    assert {r.artifact_id for r in db.scalars(select(SummarySearchTerm))} == {valid.id}



def test_keyword_terms_match_original_ascii_prefix_and_cjk_character_behavior(storage):
    db, _, source, _ = storage
    artifact(db, source, summary="Architecture 知识库检索")
    index.ensure_summary_index(db); index.index_summary_once(db)
    terms = set(db.scalars(select(SummarySearchTerm.term)))
    assert {"architecture", "arch", "ar", "知", "识", "知识", "检", "索", "检索"}.issubset(terms)
    assert "a" not in terms
