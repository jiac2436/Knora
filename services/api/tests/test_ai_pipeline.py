"""The product completes only after current local retrieval data is ready."""
from dataclasses import replace
from datetime import timedelta
import json
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from app import semantic, summary_index
from app.ai import context_state
from app.ai_pipeline import pipeline_view
from app.database import Base, make_engine
from app.models import (AIArtifact, AIJob, Domain, EvidenceChunk, EvidenceEmbedding,
                        SemanticIndexJob, Source, SourceRevision, SummaryIndexJob,
                        SummarySearchTerm, User, utcnow)


@pytest.fixture
def data(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'pipeline.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(semantic, "settings", replace(semantic.settings, semantic_enabled=False))
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        user = User(email="pipeline@example.test", name="Test")
        db.add(user); db.flush()
        domain = Domain(owner_id=user.id, name="项目资料")
        db.add(domain); db.flush()
        source = Source(owner_id=user.id, domain_id=domain.id, title="索引设计",
                        kind="text", coverage="full_text", processing_status="usable")
        db.add(source); db.flush()
        revision = SourceRevision(source_id=source.id, version=1, extracted_text="失败时仅重建本地索引。")
        db.add(revision); db.flush()
        source.current_revision_id = revision.id
        evidence = EvidenceChunk(revision_id=revision.id, ordinal=0,
                                 text=revision.extracted_text, locator_end=1)
        db.add(evidence); db.flush()
        snapshot = {"request_id": str(uuid4()), "contract_version": 2, "phase": "single",
            "kind": "analyze", "source_id": source.id, "revision_id": revision.id,
            "context": context_state(db, source), "evidence": [{"id": evidence.id,
                "text": evidence.text, "locator_kind": "paragraph", "locator_start": 0, "locator_end": 1}]}
        job = AIJob(owner_id=user.id, source_id=source.id, kind="analyze", status="completed",
                    request_key=str(uuid4()), request_sha256="a" * 64, snapshot_json=json.dumps(snapshot))
        db.add(job); db.flush()
        artifact = AIArtifact(owner_id=user.id, source_id=source.id, revision_id=revision.id,
            job_id=job.id, snapshot_json=json.dumps(snapshot), result_json=json.dumps({
                "contract_version": 2, "overview": {"text": "本地索引恢复", "citations": [evidence.id]},
                "summary": "索引失败仅需本地重建。", "summary_citations": [evidence.id]}),
            provider="test", model="test", prompt_version="test", usage_json="{}")
        db.add(artifact); db.commit()
        yield db, source, job, artifact
    engine.dispose()


def finish_summary(db):
    summary_index.ensure_summary_index(db)
    summary_index.index_summary_once(db)


def test_model_completion_waits_for_index_and_actual_data(data):
    db, source, job, artifact = data
    assert pipeline_view(db, job=job)["pipeline_status"] == "indexing"
    finish_summary(db)
    assert pipeline_view(db, artifact=artifact) == {
        "pipeline_status": "completed", "index_state": "ready", "index_retryable": False}
    db.execute(delete(SummarySearchTerm).where(SummarySearchTerm.artifact_id == artifact.id)); db.commit()
    assert db.get(SummaryIndexJob, source.id).status == "completed"
    assert pipeline_view(db, job=job)["pipeline_status"] == "indexing"


@pytest.mark.parametrize("status", ["pending", "running", "failed", "unknown", "cancelled"])
def test_operational_job_states(data, status):
    db, _, job, _ = data
    job.status = status
    expected = "analyzing" if status in {"pending", "running"} else status
    assert pipeline_view(db, job=job)["pipeline_status"] == expected


@pytest.mark.parametrize("mutation", ["title", "delete", "revision", "segment", "newer"])
def test_obsolete_or_segment_results_cannot_claim_completed(data, mutation):
    db, source, job, artifact = data
    finish_summary(db)
    if mutation == "title": source.title = "改过标题"
    elif mutation == "delete": source.deleted_at = utcnow()
    elif mutation == "revision":
        revision = SourceRevision(source_id=source.id, version=2, extracted_text="新正文")
        db.add(revision); db.flush(); source.current_revision_id = revision.id
    elif mutation == "segment":
        snapshot = json.loads(artifact.snapshot_json); snapshot["phase"] = "segment"
        artifact.snapshot_json = json.dumps(snapshot)
    else:
        db.add(AIArtifact(owner_id=artifact.owner_id, source_id=artifact.source_id,
            revision_id=artifact.revision_id, job_id=str(uuid4()), snapshot_json=artifact.snapshot_json,
            result_json=artifact.result_json, provider="test", model="test", prompt_version="test",
            usage_json="{}", created_at=artifact.created_at + timedelta(seconds=2)))
    db.commit()
    assert pipeline_view(db, job=job)["pipeline_status"] == ("failed" if mutation == "segment" else "stale")
    assert pipeline_view(db, artifact=artifact)["pipeline_status"] == "stale"


def test_failed_summary_rebuild_is_local_and_preserves_ai_job(data, monkeypatch):
    db, source, job, artifact = data
    finish_summary(db)
    index_job = db.get(SummaryIndexJob, source.id)
    index_job.status, index_job.last_error = "failed", "RuntimeError"
    db.commit()
    assert pipeline_view(db, job=job)["pipeline_status"] == "index_failed"
    monkeypatch.setattr(semantic, "embed_documents", lambda _: pytest.fail("Disabled semantic model must not run"))
    assert summary_index.rebuild_summary_index(db, source.owner_id, source.id)["queued"] == 1
    assert pipeline_view(db, job=job)["pipeline_status"] == "indexing"
    summary_index.index_summary_once(db)
    assert pipeline_view(db, job=job)["pipeline_status"] == "completed"
    assert list(db.scalars(select(AIJob.id))) == [job.id]
    assert list(db.scalars(select(AIArtifact.id))) == [artifact.id]


@pytest.mark.parametrize("index_failed", [False, True])
def test_long_document_final_result_not_segment_determines_pipeline(data, index_failed):
    db, source, job, artifact = data
    result = json.loads(artifact.result_json)
    snapshot = json.loads(artifact.snapshot_json)
    wire = {**snapshot, "phase": "synthesis", "section_summaries": [{
        "section_id": 1, "overview": result["overview"], "summary": result["summary"],
        "summary_citations": result["summary_citations"]}]}
    artifact.snapshot_json = json.dumps({**snapshot, "phase": "synthesis", "analysis_request": wire})
    db.add(AIArtifact(owner_id=artifact.owner_id, source_id=source.id,
        revision_id=artifact.revision_id, job_id=str(uuid4()),
        snapshot_json=json.dumps({**snapshot, "phase": "segment"}), result_json=artifact.result_json,
        provider="test", model="test", prompt_version="test", usage_json="{}",
        created_at=artifact.created_at + timedelta(seconds=1)))
    db.commit()
    finish_summary(db)
    if index_failed:
        db.get(SummaryIndexJob, source.id).status = "failed"; db.commit()
    assert pipeline_view(db, job=job)["pipeline_status"] == ("index_failed" if index_failed else "completed")
    assert summary_index.current_summary_artifact(db, source).id == artifact.id


def test_compact_v3_summary_participates_in_pipeline(data):
    db, _, job, artifact = data
    snapshot = json.loads(artifact.snapshot_json); snapshot["contract_version"] = 3
    result = json.loads(artifact.result_json); result["contract_version"] = 3
    artifact.snapshot_json, artifact.result_json = json.dumps(snapshot), json.dumps(result)
    db.commit()
    finish_summary(db)
    assert pipeline_view(db, job=job)["pipeline_status"] == "completed"


@pytest.mark.parametrize("state", ["missing", "pending", "failed", "wrong_revision", "missing_data"])
def test_original_semantic_must_be_current_and_ready(data, monkeypatch, state):
    db, source, job, _ = data
    monkeypatch.setattr(semantic, "settings", replace(semantic.settings, semantic_enabled=True))
    monkeypatch.setattr(semantic, "embed_documents", lambda texts: [b"synthetic-vector"] * len(texts))
    finish_summary(db)
    if state != "missing":
        db.add(SemanticIndexJob(source_id=source.id,
            revision_id=str(uuid4()) if state == "wrong_revision" else source.current_revision_id,
            status="completed" if state in {"wrong_revision", "missing_data"} else state))
        db.commit()
    assert pipeline_view(db, job=job)["pipeline_status"] == ("index_failed" if state == "failed" else "indexing")
    summary_index.rebuild_summary_index(db, source.owner_id, source.id)
    semantic_job = db.get(SemanticIndexJob, source.id)
    assert semantic_job.status == "pending" and semantic_job.revision_id == source.current_revision_id
    summary_index.index_summary_once(db)
    semantic.index_one(db)
    assert pipeline_view(db, job=job)["pipeline_status"] == "completed"
    assert len(list(db.scalars(select(AIJob)))) == 1
    db.execute(delete(EvidenceEmbedding).where(EvidenceEmbedding.source_id == source.id)); db.commit()
    assert pipeline_view(db, job=job)["pipeline_status"] == "indexing"
