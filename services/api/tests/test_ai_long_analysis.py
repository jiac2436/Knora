"""Long document calls use checkpoints and synthetic gateway responses only."""
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select

from app import ai
from app.errors import ApiError
from app.models import AIArtifact, AIJob, AIJobStep, EvidenceChunk, PersonalContext
from services.worker import ai_analysis as worker
from test_ai_analysis import storage


def long_source(data, count=3, chars=20000):
    db, _, source, chunk, _, _, _, _ = data
    db.execute(delete(EvidenceChunk).where(EvidenceChunk.revision_id == source.current_revision_id))
    db.add_all([EvidenceChunk(revision_id=source.current_revision_id, ordinal=index, text="文" * chars,
        locator_kind="text", locator_start=index * chars, locator_end=(index + 1) * chars) for index in range(count)])
    db.commit()


def preview(data, kind="analyze"):
    return ai.preview_plan(ai.PlanRequest(source_id=data[2].id, kind=kind), data[6], data[0])


def submit(data, kind="analyze", plan=None):
    plan = plan or preview(data, kind)
    return ai.create_job(ai.JobRequest(source_id=data[2].id, kind=kind, request_key=uuid4(), plan_hash=plan["plan_hash"]), data[6], data[0])


def response_for(request):
    wire = json.loads(request.content)
    evidence = wire["evidence"][0]["id"]
    return httpx.Response(200, json={"result": {"contract_version": 2,
        "overview": {"text": "Overview", "citations": [evidence]}, "summary": "Grounded summary", "summary_citations": [evidence],
        "insights": [{"text": "Grounded finding", "citations": [evidence]}]},
        "provider": "synthetic", "model": "synthetic", "prompt_version": "v2",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


def test_long_plan_requires_confirmation_and_freezes_stable_hash(storage):
    long_source(storage)
    plan = preview(storage)
    assert plan == preview(storage)
    assert plan["calls"] == 4 and plan["sections"] == 3 and plan["coverage"] == "full"
    assert plan["covered_chars"] == plan["total_chars"] == 60000
    with pytest.raises(ApiError) as error:
        ai.create_job(ai.JobRequest(source_id=storage[2].id, kind="analyze", request_key=uuid4()), storage[6], storage[0])
    assert error.value.code == "ai_plan_confirmation_required"
    job = submit(storage, plan=plan)
    assert job["total_steps"] == 4 and job["completed_steps"] == 0
    assert ai.get_job(job["id"], storage[6], storage[0])["steps"][0]["status"] == "pending"


def test_changed_document_invalidates_confirmed_plan(storage):
    long_source(storage)
    plan = preview(storage)
    storage[2].title = "Changed"; storage[0].commit()
    with pytest.raises(ApiError) as error:
        submit(storage, plan=plan)
    assert error.value.code == "ai_plan_changed"
    assert storage[0].scalar(select(AIJob)) is None


def test_completed_segments_are_persisted_once_and_synthesis_uses_original_ids(storage):
    long_source(storage)
    db = storage[0]
    job = submit(storage)
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        return response_for(request)
    transport = httpx.MockTransport(handle)
    for index in range(4):
        assert worker.run_ai_once(db, transport)
        db.expire_all()
        assert db.get(AIJob, job["id"]).status == ("pending" if index < 3 else "completed")
    assert not worker.run_ai_once(db, transport)
    assert len(requests) == 4
    assert len({r["request_id"] for r in requests}) == 4
    assert [r["phase"] for r in requests] == ["segment"] * 3 + ["synthesis"]
    assert all(not r["tags"] and not r["domains"] for r in requests[:3])
    final = requests[-1]
    assert [memo["section_id"] for memo in final["section_summaries"]] == [1, 2, 3]
    assert {item["id"] for item in final["evidence"]} == {r["evidence"][0]["id"] for r in requests[:3]}
    assert all(len(item["text"]) <= 500 for item in final["evidence"])
    artifacts = db.scalars(select(AIArtifact)).all()
    assert len(artifacts) == 4
    result = db.scalar(select(AIArtifact).where(AIArtifact.job_id == job["id"]))
    assert json.loads(result.usage_json) == {"prompt_tokens": 40, "completion_tokens": 20}
    assert ai.artifact_view(db, result)["phase"] == "synthesis"
    assert ai.get_job(job["id"], storage[6], db)["completed_steps"] == 4


def test_partial_plan_does_not_claim_complete_original(storage):
    long_source(storage, count=9, chars=24000)
    storage[2].coverage = "partial_text"; storage[0].commit()
    plan = preview(storage)
    assert plan["sections"] == 8 and plan["calls"] == 9
    assert plan["covered_chars"] == 192000 and plan["total_chars"] == 216000
    assert plan["coverage"] == "partial" and plan["source_coverage"] == "partial_text"


def test_one_oversized_original_chunk_is_sliced_without_losing_original_mapping(storage):
    long_source(storage, count=1, chars=60000)
    plan = preview(storage)
    assert plan["sections"] == 3 and plan["calls"] == 4
    assert plan["coverage"] == "full" and plan["covered_chars"] == 60000
    assert plan == preview(storage)
    db = storage[0]
    original = db.scalar(select(EvidenceChunk))
    job = submit(storage, plan=plan)
    snapshot = json.loads(db.get(AIJob, job["id"]).snapshot_json)
    slices = snapshot["evidence"]
    assert [item["text_start"] for item in slices] == [0, 24000, 48000]
    assert [item["text_end"] for item in slices] == [24000, 48000, 60000]
    assert len({item["id"] for item in slices}) == 3
    assert all(item["original_evidence_id"] == original.id for item in slices)
    assert all(item["text"] == original.text[item["text_start"]:item["text_end"]] for item in slices)
    assert len(db.scalars(select(EvidenceChunk)).all()) == 1
    wires = []
    def handle(request):
        wires.append(json.loads(request.content))
        return response_for(request)
    for _ in range(4):
        worker.run_ai_once(db, httpx.MockTransport(handle))
    final = wires[-1]
    assert {item["id"] for item in final["evidence"]} == {item["id"] for item in slices}
    assert all(item["text_end"] - item["text_start"] == len(item["text"]) <= 500 for item in final["evidence"])
    assert all(item["original_evidence_id"] == original.id for item in final["evidence"])
    artifact = db.scalar(select(AIArtifact).where(AIArtifact.job_id == job["id"]))
    assert ai.artifact_view(db, artifact)["coverage"] == "full"
    assert db.get(AIJob, job["id"]).status == "completed"


def test_unknown_step_stops_future_calls_and_cannot_resume(storage):
    long_source(storage)
    db = storage[0]; job = submit(storage)
    calls = []
    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic", request=request)
    assert worker.run_ai_once(db, httpx.MockTransport(handle))
    assert db.get(AIJob, job["id"]).status == "unknown"
    assert db.scalar(select(AIJobStep).where(AIJobStep.ordinal == 0)).status == "unknown"
    assert not worker.run_ai_once(db, httpx.MockTransport(handle))
    with pytest.raises(ApiError) as error:
        ai.resume_job(job["id"], storage[6], db)
    assert error.value.code == "ai_resume_not_safe" and len(calls) == 1


def test_cancel_during_call_retains_paid_segment_but_stops_remaining(storage):
    long_source(storage)
    db, sessions, _, _, _, _, owner, _ = storage
    job = submit(storage)
    def handle(request):
        with sessions() as other:
            ai.cancel_job(job["id"], owner, other)
        return response_for(request)
    worker.run_ai_once(db, httpx.MockTransport(handle))
    db.expire_all()
    assert db.get(AIJob, job["id"]).status == "cancelled"
    assert db.scalar(select(AIJobStep).where(AIJobStep.ordinal == 0)).status == "completed"
    artifact = db.scalar(select(AIArtifact))
    assert artifact and ai.artifact_view(db, artifact)["phase"] == "segment"
    assert not worker.run_ai_once(db, httpx.MockTransport(response_for))


def test_edit_between_steps_stops_without_new_paid_call(storage):
    long_source(storage)
    db = storage[0]; job = submit(storage)
    worker.run_ai_once(db, httpx.MockTransport(response_for))
    db.add(PersonalContext(source_id=storage[2].id, notes="New personal opinion")); db.commit()
    calls = []
    worker.run_ai_once(db, httpx.MockTransport(lambda request: calls.append(request)))
    assert not calls
    assert db.get(AIJob, job["id"]).status == "cancelled"
    assert len(db.scalars(select(AIArtifact)).all()) == 1


def test_resume_pending_checks_owner_and_does_not_replay_completed_step(storage):
    long_source(storage)
    db = storage[0]; job = submit(storage)
    worker.run_ai_once(db, httpx.MockTransport(response_for))
    assert ai.resume_job(job["id"], storage[6], db)["completed_steps"] == 1
    with pytest.raises(ApiError) as error:
        ai.get_job(job["id"], storage[7], db)
    assert error.value.status_code == 404
    worker.run_ai_once(db, httpx.MockTransport(response_for))
    assert ai.get_job(job["id"], storage[6], db)["completed_steps"] == 2


def test_short_task_still_uses_one_call(storage):
    plan = preview(storage)
    assert plan["calls"] == 1 and plan["sections"] == 0
    job = ai.create_job(ai.JobRequest(source_id=storage[2].id, kind="analyze", request_key=uuid4()), storage[6], storage[0])
    worker.run_ai_once(storage[0], httpx.MockTransport(response_for))
    assert storage[0].get(AIJob, job["id"]).status == "completed"
    assert storage[0].scalar(select(AIJobStep)) is None


def test_large_personal_context_rejected_before_paid_task(storage):
    long_source(storage)
    db = storage[0]
    db.add(PersonalContext(source_id=storage[2].id, notes="😀" * 150000)); db.commit()
    with pytest.raises(ApiError) as error:
        preview(storage)
    assert error.value.code == "ai_request_too_large"
    assert db.scalar(select(AIJob)) is None


def test_final_checkpoint_recovers_publication_without_repeating_paid_call(storage, monkeypatch):
    long_source(storage)
    db = storage[0]; job = submit(storage)
    for _ in range(3):
        worker.run_ai_once(db, httpx.MockTransport(response_for))
    def interrupted(*args, **kwargs):
        raise RuntimeError("Synthetic crash after checkpoint commit")
    with monkeypatch.context() as patch:
        patch.setattr(worker, "_finish", interrupted)
        with pytest.raises(RuntimeError):
            worker.run_ai_once(db, httpx.MockTransport(response_for))
    db.expire_all()
    persisted = db.get(AIJob, job["id"])
    assert persisted.status == "running"
    assert all(step.status == "completed" for step in db.scalars(select(AIJobStep)))
    # Simulate the startup guard. No provider request remains unknown here.
    persisted.status = "unknown"; db.commit()
    assert ai.get_job(job["id"], storage[6], db)["can_resume"]
    ai.resume_job(job["id"], storage[6], db)
    calls = []
    worker.run_ai_once(db, httpx.MockTransport(lambda request: calls.append(request)))
    assert not calls
    artifact = db.scalar(select(AIArtifact).where(AIArtifact.job_id == job["id"]))
    assert artifact and json.loads(artifact.usage_json) == {"prompt_tokens": 40, "completion_tokens": 20}
    assert db.get(AIJob, job["id"]).status == "completed"


def test_synthesis_organization_applies_against_final_wire_evidence(storage):
    long_source(storage)
    db, _, source, _, domain, project, owner, _ = storage
    job = submit(storage, kind="organize")
    def handle(request):
        wire = json.loads(request.content)
        reply = response_for(request)
        if wire["phase"] == "synthesis":
            body = json.loads(reply.content)
            body["result"]["organizing_suggestion"] = {"domain_id": domain.id, "project_ids": [project.id],
                "reason": "Grounded organization", "citations": [wire["evidence"][0]["id"]]}
            return httpx.Response(200, json=body)
        return reply
    for _ in range(4):
        worker.run_ai_once(db, httpx.MockTransport(handle))
    artifact = db.scalar(select(AIArtifact).where(AIArtifact.job_id == job["id"]))
    assert ai.apply_artifact(artifact.id, owner, db)["applied_at"]
    assert source.domain_id == domain.id


def test_final_inflight_cancel_keeps_checkpoint_without_publishing_final(storage):
    long_source(storage)
    db, sessions, _, _, _, _, owner, _ = storage
    job = submit(storage)
    for _ in range(3):
        worker.run_ai_once(db, httpx.MockTransport(response_for))
    def handle(request):
        with sessions() as other:
            ai.cancel_job(job["id"], owner, other)
        return response_for(request)
    worker.run_ai_once(db, httpx.MockTransport(handle))
    db.expire_all()
    assert db.get(AIJob, job["id"]).status == "cancelled"
    assert db.scalar(select(AIArtifact).where(AIArtifact.job_id == job["id"])) is None
    assert all(step.status == "completed" for step in db.scalars(select(AIJobStep)))
    assert not ai.get_job(job["id"], owner, db)["can_resume"]


def test_segment_invalid_citation_preserves_no_false_checkpoint(storage):
    long_source(storage)
    db = storage[0]; job = submit(storage)
    def handle(request):
        body = json.loads(response_for(request).content)
        body["result"]["summary_citations"] = ["foreign-evidence"]
        return httpx.Response(200, json=body)
    worker.run_ai_once(db, httpx.MockTransport(handle))
    assert db.get(AIJob, job["id"]).status == "failed"
    assert db.scalar(select(AIJobStep).where(AIJobStep.ordinal == 0)).status == "failed"
    assert db.scalar(select(AIArtifact)) is None


def test_claim_interruption_can_resume_only_unsent_steps(storage):
    long_source(storage)
    db = storage[0]; job = submit(storage)
    sent = []
    def handle(request):
        sent.append(json.loads(request.content)["request_id"])
        return response_for(request)
    worker.run_ai_once(db, httpx.MockTransport(handle))
    completed_id = sent[0]
    persisted = db.get(AIJob, job["id"])
    # The next iteration claims the parent before checkpointing its HTTP attempt.
    persisted.status = "running"; db.commit()
    persisted.status = "unknown"; db.commit()  # Startup interruption guard.
    assert ai.get_job(job["id"], storage[6], db)["can_resume"]
    ai.resume_job(job["id"], storage[6], db)
    for _ in range(3):
        worker.run_ai_once(db, httpx.MockTransport(handle))
    assert sent.count(completed_id) == 1 and len(set(sent)) == len(sent) == 4
    assert db.get(AIJob, job["id"]).status == "completed"


@pytest.mark.parametrize("attempted_status", ["running", "unknown", "failed"])
def test_interrupted_parent_cannot_resume_attempted_step(storage, attempted_status):
    long_source(storage)
    db = storage[0]; job = submit(storage)
    persisted = db.get(AIJob, job["id"])
    step = db.scalar(select(AIJobStep).where(AIJobStep.job_id == persisted.id, AIJobStep.ordinal == 0))
    step.status = attempted_status; persisted.status = "unknown"; db.commit()
    assert not ai.get_job(job["id"], storage[6], db)["can_resume"]
    with pytest.raises(ApiError) as error:
        ai.resume_job(job["id"], storage[6], db)
    assert error.value.code == "ai_resume_not_safe"


def test_single_chunk_larger_than_long_budget_is_explicitly_partial(storage):
    long_source(storage, count=1, chars=200000)
    plan = preview(storage)
    assert plan["sections"] == 8 and plan["calls"] == 9
    assert plan["covered_chars"] == 192000 and plan["total_chars"] == 200000
    assert plan["coverage"] == "partial"
