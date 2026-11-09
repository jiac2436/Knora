"""Local tag decisions use synthetic results and never invoke a paid model."""
import json

import httpx
import pytest
from sqlalchemy import select

from app import ai, ai_tag_suggestions as suggestions, knowledge_tags as tags
from app.errors import ApiError
from app.models import AIArtifact, AIJob, KnowledgeTag, PersonalContext, SourceRevision, TagAssignment, TagRejection, TagSuggestion
from services.worker import ai_analysis as worker
from test_ai_analysis import storage, create, response


def result(data, name="RAG", dimension="topic", tag_id=None):
    body = response(data)
    body["result"]["tag_suggestions"] = [{"name": name, "dimension": dimension, "tag_id": tag_id,
        "reason": "Source describes retrieval", "citations": [data[3].id]}]
    return body


def publish(data, body=None, handler=None):
    create(data)
    body = body or result(data)
    worker.run_ai_once(data[0], httpx.MockTransport(handler or (lambda _: httpx.Response(200, json=body))))
    return data[0].scalar(select(TagSuggestion).order_by(TagSuggestion.created_at.desc()))


def test_new_topic_stays_pending_then_confirmed_locally(storage):
    db, _, source, chunk, _, _, owner, _ = storage
    candidate = publish(storage)
    assert candidate.status == "pending"
    assert db.scalar(select(KnowledgeTag)) is None
    assert db.scalar(select(TagAssignment)) is None
    view = suggestions.accept_suggestion(candidate.id, owner, db)
    assert view["status"] == "accepted" and view["citations"] == [chunk.id]
    assignment = db.get(TagAssignment, (source.id, view["tag_id"], "ai"))
    assert assignment.revision_id == source.current_revision_id
    assert assignment.artifact_id == candidate.artifact_id
    artifact = db.get(AIArtifact, candidate.artifact_id)
    assert ai.artifact_view(db, artifact)["tag_suggestions"][0]["status"] == "accepted"


def test_manual_and_ai_associations_survive_independently(storage):
    db, _, source, _, _, _, owner, _ = storage
    tag = tags.create_tag(tags.CreateTag(name="RAG"), owner, db)
    tags.assign_tag(source.id, tags.AssignmentRequest(tag_id=tag["id"]), owner, db)
    candidate = publish(storage, result(storage, tag_id=tag["id"]))
    suggestions.accept_suggestion(candidate.id, owner, db)
    assert set(db.scalars(select(TagAssignment.origin))) == {"manual", "ai"}
    tags.unassign_tag(source.id, tag["id"], "manual", owner, db)
    assert list(db.scalars(select(TagAssignment.origin))) == ["ai"]


def test_accepted_replay_does_not_restore_removed_assignment(storage):
    db, _, source, _, _, _, owner, _ = storage
    candidate = publish(storage)
    accepted = suggestions.accept_suggestion(candidate.id, owner, db)
    tags.unassign_tag(source.id, accepted["tag_id"], "ai", owner, db)
    source.title = "Manual later edit"; db.commit()
    assert suggestions.accept_suggestion(candidate.id, owner, db)["status"] == "accepted"
    assert db.scalar(select(TagAssignment)) is None


def test_reject_suppresses_same_revision_and_clear_requires_new_analysis(storage):
    db, _, source, _, _, _, owner, _ = storage
    first = publish(storage)
    second = publish(storage)
    suggestions.reject_suggestion(first.id, owner, db)
    assert db.get(TagSuggestion, second.id).status == "rejected"
    assert db.scalar(select(TagRejection)) is not None
    job = create(storage)
    snapshot = json.loads(db.get(AIJob, job["id"]).snapshot_json)
    assert snapshot["rejected_tags"] == [{"dimension": "topic", "name": "rag"}]
    with pytest.raises(ApiError):
        suggestions.accept_suggestion(first.id, owner, db)
    cleared = suggestions.clear_rejections(source.id, None, None, owner, db)
    assert cleared == {"removed": 1}
    assert first.status == "rejected"
    worker.run_ai_once(db, httpx.MockTransport(lambda _: httpx.Response(200, json=response(storage))))
    newer = publish(storage)
    assert newer.status == "pending"


@pytest.mark.parametrize("edit", ["revision", "context", "title"])
def test_late_result_is_kept_but_cannot_apply_after_edit(storage, edit):
    db, _, source, _, _, _, owner, _ = storage
    create(storage)
    if edit == "revision":
        revision = SourceRevision(source_id=source.id, version=2, extracted_text="Changed")
        db.add(revision); db.flush(); source.current_revision_id = revision.id
    elif edit == "context":
        db.add(PersonalContext(source_id=source.id, notes="My personal idea"))
    else:
        source.title = "Changed"
    db.commit()
    worker.run_ai_once(db, httpx.MockTransport(lambda _: httpx.Response(200, json=result(storage))))
    candidate = db.scalar(select(TagSuggestion))
    assert suggestions.suggestion_view(db, candidate)["stale"]
    with pytest.raises(ApiError) as error:
        suggestions.accept_suggestion(candidate.id, owner, db)
    assert error.value.code == "ai_artifact_stale"
    assert db.scalar(select(TagAssignment)) is None


def test_owner_cannot_access_foreign_suggestion(storage):
    candidate = publish(storage)
    with pytest.raises(ApiError) as error:
        suggestions.accept_suggestion(candidate.id, storage[7], storage[0])
    assert error.value.status_code == 404


def test_v1_saved_task_still_completes(storage):
    db = storage[0]
    created = create(storage)
    job = db.get(AIJob, created["id"])
    snapshot = json.loads(job.snapshot_json)
    for field in ("contract_version", "tags", "rejected_tags"):
        snapshot.pop(field)
    job.snapshot_json = ai.canonical(snapshot); db.commit()
    body = response(storage)
    body["result"].pop("contract_version"); body["result"].pop("overview")
    worker.run_ai_once(db, httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    assert job.status == "completed"
    assert db.scalar(select(TagSuggestion)) is None


def test_rejection_during_remote_call_blocks_late_candidate(storage):
    db, sessions, source, _, _, _, owner, _ = storage
    old = publish(storage)
    def handle(_):
        with sessions() as concurrent:
            suggestions.reject_suggestion(old.id, owner, concurrent)
        return httpx.Response(200, json=result(storage))
    new = publish(storage, handler=handle)
    assert new.status == "rejected"
    assert db.scalar(select(TagAssignment)) is None


def test_renamed_taxonomy_during_remote_call_drops_candidate(storage):
    db, sessions, _, _, _, _, owner, _ = storage
    tag = tags.create_tag(tags.CreateTag(name="RAG"), owner, db)
    body = result(storage, tag_id=tag["id"])
    def handle(_):
        with sessions() as concurrent:
            tags.patch_tag(tag["id"], tags.PatchTag(name="Retrieval"), owner, concurrent)
        return httpx.Response(200, json=body)
    assert publish(storage, body, handle) is None
    artifact = db.scalar(select(AIArtifact))
    assert "标签目录已变更" in json.loads(artifact.result_json)["warnings"][-1]


def test_alias_rejection_suppresses_same_standard_tag(storage):
    db, _, source, _, _, _, owner, _ = storage
    tag = tags.create_tag(tags.CreateTag(name="检索增强生成"), owner, db)
    tags.add_alias(tag["id"], tags.AliasRequest(name="RAG"), owner, db)
    suggestions.record_rejection(db, source, "topic", "RAG", None); db.commit()
    assert suggestions.rejection_matches(db, source, "topic", "检索增强生成", tag["id"])


def test_tag_catalog_overflow_fails_before_task_creation(storage):
    db, _, source, _, _, _, _, _ = storage
    db.add_all([KnowledgeTag(owner_id=source.owner_id, name=f"Tag {i}", normalized_name=f"tag {i}", dimension="topic") for i in range(501)])
    db.commit()
    with pytest.raises(ApiError) as error:
        create(storage)
    assert error.value.code == "ai_taxonomy_too_large"
    assert db.scalar(select(AIJob)) is None


def test_candidate_http_routes_accept_and_reject(client, account, monkeypatch):
    from dataclasses import replace
    from uuid import uuid4
    from app.database import engine
    from sqlalchemy.orm import Session
    configured = replace(ai.settings, ai_gateway_url="http://127.0.0.1:8765", ai_gateway_token="synthetic-secret")
    monkeypatch.setattr(ai, "settings", configured)
    monkeypatch.setattr(worker, "settings", configured)
    headers = account["headers"]
    capture = client.post("/v1/captures", headers={**headers, "Idempotency-Key": "tag-ai-http"}, json={
        "capture_type": "text", "title": "Synthetic", "coverage": "full_text", "content": {"text": "RAG evidence"},
        "context": {"domain_id": account["domain"]["id"]}, "client": {"kind": "desktop", "version": "test"}})
    source_id = capture.json()["id"]
    assert client.post("/v1/ai/jobs", headers=headers, json={"source_id": source_id,
        "kind": "analyze", "request_key": str(uuid4())}).status_code == 202
    def handle(request):
        wire = json.loads(request.content)
        evidence = wire["evidence"][0]["id"]
        return httpx.Response(200, json={"result": {"contract_version": 2,
            "overview": {"text": "Overview", "citations": [evidence]}, "summary": "Summary", "summary_citations": [evidence],
            "tag_suggestions": [{"name": name, "dimension": "topic", "tag_id": None,
                "reason": "Source topic", "citations": [evidence]} for name in ("RAG", "检索")]},
            "provider": "synthetic", "model": "synthetic", "prompt_version": "v2",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    with Session(engine) as db:
        worker.run_ai_once(db, httpx.MockTransport(handle))
    artifacts = client.get("/v1/ai/artifacts", headers=headers, params={"source_id": source_id}).json()["items"]
    candidates = artifacts[0]["tag_suggestions"]
    accepted = client.post(f"/v1/ai/tag-suggestions/{candidates[0]['id']}/accept", headers=headers)
    assert accepted.status_code == 200 and accepted.json()["status"] == "accepted"
    rejected = client.post(f"/v1/ai/tag-suggestions/{candidates[1]['id']}/reject", headers=headers)
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert client.delete(f"/v1/sources/{source_id}/ai-tag-rejections", headers=headers).json() == {"removed": 1}


def test_removing_ai_label_suppresses_future_same_version(storage):
    db, _, source, _, _, _, owner, _ = storage
    candidate = publish(storage)
    view = suggestions.accept_suggestion(candidate.id, owner, db)
    tags.unassign_tag(source.id, view["tag_id"], "ai", owner, db)
    assert suggestions.rejection_matches(db, source, "topic", "RAG", view["tag_id"])
    assert db.scalar(select(TagAssignment)) is None


def test_known_tag_renamed_after_publication_cannot_accept(storage):
    db, _, _, _, _, _, owner, _ = storage
    tag = tags.create_tag(tags.CreateTag(name="RAG"), owner, db)
    candidate = publish(storage, result(storage, tag_id=tag["id"]))
    tags.patch_tag(tag["id"], tags.PatchTag(name="New name"), owner, db)
    with pytest.raises(ApiError) as error:
        suggestions.accept_suggestion(candidate.id, owner, db)
    assert error.value.code == "ai_candidate_changed"
    assert db.scalar(select(TagAssignment)) is None
