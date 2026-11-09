"""Compact analysis publishes machine labels atomically without user approval."""
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app import ai, knowledge_tags as tags, ai_tag_suggestions as suggestions
from app.errors import ApiError
from app.models import AIArtifact, AIJob, TagAssignment, TagSuggestion, SummaryIndexJob, SourceRevision, PersonalContext
from services.worker import ai_analysis as worker
from test_ai_analysis import storage, response


def create_product(data):
    return ai.create_job(ai.JobRequest(source_id=data[2].id, kind="analyze", request_key=uuid4(), contract_version=3), data[6], data[0])


def result(data, name="RAG", tag_id=None):
    body = response(data)
    body["result"].update(contract_version=3, overview={"text": "知识检索资料", "citations": [data[3].id]},
        summary="知识检索资料", insights=[], organizing_suggestion=None,
        tag_suggestions=[{"name": name, "tag_id": tag_id, "dimension": "topic", "reason": "相关主题", "citations": [data[3].id]}])
    return body


def complete(data, body=None, handler=None):
    return worker.run_ai_once(data[0], httpx.MockTransport(handler or (lambda _: httpx.Response(200, json=body or result(data)))))


def test_auto_labels_preserve_manual_and_enqueue_index(storage):
    db, _, source, _, _, _, owner, _ = storage
    tag = tags.create_tag(tags.CreateTag(name="RAG"), owner, db)
    tags.assign_tag(source.id, tags.AssignmentRequest(tag_id=tag["id"]), owner, db)
    job = create_product(storage)
    complete(storage, result(storage, tag_id=tag["id"]))
    artifact = db.scalar(select(AIArtifact))
    assert set(db.scalars(select(TagAssignment.origin))) == {"manual", "ai"}
    assert db.get(SummaryIndexJob, source.id).artifact_id == artifact.id
    assert db.scalar(select(TagSuggestion)).status == "accepted"
    assert ai.get_job(job["id"], owner, db)["pipeline_status"] != "completed"


def test_known_invalid_provider_output_is_failed_without_automatic_retry(storage):
    created = create_product(storage)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(502, json={"detail": {"code": "invalid_provider_result",
            "stage": "finish_reason", "message": "synthetic-private-message"}})
    complete(storage, handler=handler)
    job = storage[0].get(AIJob, created["id"])
    assert job.status == "failed" and "可能已产生用量" in job.last_error
    assert "synthetic-private-message" not in job.last_error
    assert not worker.run_ai_once(storage[0], httpx.MockTransport(handler))
    assert len(calls) == 1


def test_same_snapshot_pending_reuses_but_completed_allows_reanalysis(storage):
    first = create_product(storage)
    assert create_product(storage)["id"] == first["id"]
    complete(storage)
    second = create_product(storage)
    assert second["id"] != first["id"]
    complete(storage, result(storage, "Retrieval"))
    db = storage[0]
    assert len(db.scalars(select(AIArtifact)).all()) == 2
    assert len(db.scalars(select(TagAssignment).where(TagAssignment.origin == "ai")).all()) == 1


def test_user_correction_during_call_not_restored(storage):
    db, sessions, source, _, _, _, owner, _ = storage
    create_product(storage); complete(storage)
    old = db.scalar(select(TagAssignment))
    tag_id = old.tag_id
    create_product(storage)
    def handler(_):
        with sessions() as concurrent:
            tags.unassign_tag(source.id, tag_id, "ai", owner, concurrent)
        return httpx.Response(200, json=result(storage, tag_id=tag_id))
    complete(storage, handler=handler)
    assert db.scalar(select(TagAssignment)) is None


@pytest.mark.parametrize("edit", ["title", "revision", "context", "cancel", "catalog"])
def test_late_result_does_not_publish(storage, edit):
    db, sessions, source, _, _, _, owner, _ = storage
    tag = tags.create_tag(tags.CreateTag(name="RAG"), owner, db)
    job = create_product(storage)
    def handler(_):
        with sessions() as concurrent:
            if edit == "title":
                item = concurrent.get(type(source), source.id); item.title = "Changed"; concurrent.commit()
            elif edit == "revision":
                revision = SourceRevision(source_id=source.id, version=2, extracted_text="Changed")
                concurrent.add(revision); concurrent.flush()
                concurrent.get(type(source), source.id).current_revision_id = revision.id; concurrent.commit()
            elif edit == "context":
                concurrent.add(PersonalContext(source_id=source.id, notes="Changed")); concurrent.commit()
            elif edit == "catalog":
                tags.patch_tag(tag["id"], tags.PatchTag(name="Changed"), owner, concurrent)
            else:
                ai.cancel_job(job["id"], owner, concurrent)
        return httpx.Response(200, json=result(storage, tag_id=tag["id"]))
    complete(storage, handler=handler)
    assert db.get(AIJob, job["id"]).status == "cancelled"
    assert db.scalar(select(AIArtifact)) is None
    assert db.scalar(select(TagAssignment)) is None


def test_v2_still_requires_manual_acceptance(storage):
    job = ai.create_job(ai.JobRequest(source_id=storage[2].id, kind="analyze", request_key=uuid4()), storage[6], storage[0])
    body = result(storage); body["result"]["contract_version"] = 2
    complete(storage, body)
    assert storage[0].scalar(select(TagSuggestion)).status == "pending"
    assert storage[0].scalar(select(TagAssignment)) is None


def test_unreadable_and_organize_rejected_before_paid_request(storage):
    db = storage[0]
    storage[3].text = "... !!!"; db.commit()
    with pytest.raises(ApiError) as error:
        create_product(storage)
    assert error.value.code == "ai_evidence_unreadable"
    with pytest.raises(ApiError) as error:
        ai.create_job(ai.JobRequest(source_id=storage[2].id, kind="organize", request_key=uuid4(), contract_version=3), storage[6], db)
    assert error.value.code == "ai_kind_invalid"
    assert db.scalar(select(AIJob)) is None


def test_long_product_only_final_publishes_tags(storage):
    from test_ai_long_analysis import long_source
    long_source(storage)
    db, _, source, _, _, _, owner, _ = storage
    plan = ai.preview_plan(ai.PlanRequest(source_id=source.id, kind="analyze", contract_version=3), owner, db)
    job = ai.create_job(ai.JobRequest(source_id=source.id, kind="analyze", request_key=uuid4(),
        contract_version=3, plan_hash=plan["plan_hash"]), owner, db)
    def handler(request):
        wire = json.loads(request.content)
        evidence = wire["evidence"][0]["id"]
        body = result(storage)
        body["result"]["overview"]["citations"] = [evidence]
        body["result"]["summary_citations"] = [evidence]
        body["result"]["tag_suggestions"] = [] if wire["phase"] == "segment" else [
            {"name": "RAG", "tag_id": None, "dimension": "topic", "reason": "相关主题", "citations": [evidence]}]
        return httpx.Response(200, json=body)
    for _ in range(plan["calls"] - 1):
        complete(storage, handler=handler)
        assert db.scalar(select(TagAssignment)) is None
        assert db.get(SummaryIndexJob, source.id) is None
    complete(storage, handler=handler)
    assert db.get(AIJob, job["id"]).status == "completed", db.get(AIJob, job["id"]).last_error
    assignment = db.scalar(select(TagAssignment))
    final = db.scalar(select(AIArtifact).where(AIArtifact.job_id == job["id"]))
    assert assignment.artifact_id == final.id
    assert db.get(SummaryIndexJob, source.id).artifact_id == final.id


def test_concurrent_submission_reuses_single_pending_job(storage):
    from concurrent.futures import ThreadPoolExecutor
    db, sessions, source, _, _, _, owner, _ = storage
    db.rollback()
    source_id = source.id
    def submit(_):
        with sessions() as concurrent:
            return ai.create_job(ai.JobRequest(source_id=source_id, kind="analyze", request_key=uuid4(),
                contract_version=3), owner, concurrent)["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(submit, range(2)))
    assert ids[0] == ids[1]
    assert len(db.scalars(select(AIJob)).all()) == 1


def test_reused_request_key_remains_idempotent_after_completion(storage):
    db, _, source, _, _, _, owner, _ = storage
    first = create_product(storage)
    duplicate = ai.JobRequest(source_id=source.id, kind="analyze", request_key=uuid4(), contract_version=3)
    assert ai.create_job(duplicate, owner, db)["id"] == first["id"]
    complete(storage)
    assert ai.create_job(duplicate, owner, db)["id"] == first["id"]
    assert len(db.scalars(select(AIJob)).all()) == 1
    with pytest.raises(ApiError) as error:
        ai.create_job(duplicate.model_copy(update={"plan_hash": "a" * 64}), owner, db)
    assert error.value.code == "ai_request_conflict"
    with pytest.raises(ApiError) as error:
        ai.create_job(duplicate.model_copy(update={"contract_version": 2}), owner, db)
    assert error.value.code == "ai_request_conflict"


@pytest.mark.parametrize("edit", ["title", "revision", "context"])
def test_stale_queued_single_never_calls_provider(storage, edit):
    db, _, source, _, _, _, _, _ = storage
    job = create_product(storage)
    if edit == "title":
        source.title = "Changed while queued"
    elif edit == "revision":
        revision = SourceRevision(source_id=source.id, version=2, extracted_text="Changed")
        db.add(revision); db.flush(); source.current_revision_id = revision.id
    else:
        db.add(PersonalContext(source_id=source.id, notes="Changed while queued"))
    db.commit()
    calls = []
    complete(storage, handler=lambda request: (calls.append(request), httpx.Response(200, json=result(storage)))[1])
    assert calls == []
    assert db.get(AIJob, job["id"]).status == "cancelled"
    assert db.scalar(select(AIArtifact)) is None


def test_concurrent_alias_identity_conflict_across_sources(storage):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from time import sleep
    from app.models import Source, EvidenceChunk
    db, sessions, source, _, _, _, owner, _ = storage
    other = Source(owner_id=source.owner_id, domain_id=source.domain_id,
        title="Second", kind="text", coverage="full_text")
    db.add(other); db.flush()
    revision = SourceRevision(source_id=other.id, version=1, extracted_text="Synthetic evidence")
    db.add(revision); db.flush(); other.current_revision_id = revision.id
    db.add(EvidenceChunk(revision_id=revision.id, ordinal=0, text=revision.extracted_text, locator_end=18))
    db.commit()
    source_ids = [source.id, other.id]
    for source_id in source_ids:
        ai.create_job(ai.JobRequest(source_id=source_id, kind="analyze", contract_version=3,
            request_key=uuid4()), owner, db)
    db.rollback()
    key, start = uuid4(), Barrier(2)
    def submit(source_id):
        with sessions() as concurrent:
            original = concurrent.scalars
            def slow_identity_read(statement, *args, **kwargs):
                rows = original(statement, *args, **kwargs)
                if "snapshot_json LIKE" in str(statement):
                    rows = list(rows)
                    # Expose the old read-before-lock race without blocking a
                    # correctly serialized contender at a second barrier.
                    sleep(0.05)
                return rows
            concurrent.scalars = slow_identity_read
            start.wait(timeout=5)
            try:
                return ai.create_job(ai.JobRequest(source_id=source_id, kind="analyze",
                    contract_version=3, request_key=key), owner, concurrent)["id"]
            except ApiError as error:
                return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, source_ids))
    assert outcomes.count("ai_request_conflict") == 1
    aliases = [json.loads(job.snapshot_json).get("request_aliases", []) for job in db.scalars(select(AIJob))]
    assert sum(any(alias["key"] == str(key) for alias in values) for values in aliases) == 1
