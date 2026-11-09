"""Synthetic AI transport tests; these never call a paid provider."""
from dataclasses import replace
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from app import ai
from app.auth import AppPrincipal
from app.database import Base, make_engine
from app.errors import ApiError
from app.models import AIArtifact, AIJob, Domain, EvidenceChunk, PersonalContext, Project, Source, SourceProject, SourceRevision, User, utcnow
from services.worker import ai_analysis as worker


@pytest.fixture
def storage(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'ai-test.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    configured = replace(ai.settings, ai_gateway_url="http://127.0.0.1:8765", ai_gateway_token="synthetic-secret")
    monkeypatch.setattr(ai, "settings", configured)
    monkeypatch.setattr(worker, "settings", configured)
    with sessions() as db:
        owner, other = User(email="ai@example.test", name="AI"), User(email="other@example.test", name="Other")
        db.add_all([owner, other]); db.flush()
        domain = Domain(owner_id=owner.id, name="Original")
        target = Domain(owner_id=owner.id, name="Target")
        project = Project(owner_id=owner.id, name="Project")
        db.add_all([domain, target, project]); db.flush()
        source = Source(owner_id=owner.id, domain_id=domain.id, title="Synthetic", kind="text", coverage="full_text")
        db.add(source); db.flush()
        revision = SourceRevision(source_id=source.id, version=1, extracted_text="Synthetic evidence")
        db.add(revision); db.flush()
        source.current_revision_id = revision.id
        chunk = EvidenceChunk(revision_id=revision.id, ordinal=0, text=revision.extracted_text, locator_end=18)
        db.add(chunk); db.commit()
        yield db, sessions, source, chunk, target, project, AppPrincipal(owner.id), AppPrincipal(other.id)
    engine.dispose()


def create(data, kind="organize", key=None):
    db, _, source, _, _, _, principal, _ = data
    return ai.create_job(ai.JobRequest(source_id=source.id, kind=kind, request_key=key or uuid4()), principal, db)


def response(data):
    _, _, _, chunk, domain, project, _, _ = data
    return {"result": {"contract_version": 2, "overview": {"text": "Synthetic overview", "citations": [chunk.id]}, "summary": "Synthetic summary", "summary_citations": [chunk.id],
        "insights": [{"text": "Synthetic insight", "citations": [chunk.id]}],
        "organizing_suggestion": {"domain_id": domain.id, "project_ids": [project.id],
            "reason": "Synthetic reason", "citations": [chunk.id]}, "warnings": []},
        "provider": "synthetic", "model": "synthetic-model", "prompt_version": "test-v1",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def complete(data, handler=None):
    db = data[0]
    return worker.run_ai_once(db, httpx.MockTransport(handler or (lambda _: httpx.Response(200, json=response(data)))))


def test_frozen_wire_and_success_with_local_artifact(storage):
    created = create(storage)
    calls = []
    def handle(request):
        calls.append(request)
        body = json.loads(request.content)
        assert "title" not in body and "warnings" not in body and "gateway_url" not in body
        assert body["evidence"][0]["id"] == storage[3].id
        assert request.headers["authorization"] == "Bearer synthetic-secret"
        return httpx.Response(200, json=response(storage))
    assert complete(storage, handle)
    db = storage[0]
    artifact = db.scalar(select(AIArtifact))
    assert db.get(AIJob, created["id"]).status == "completed"
    assert ai.artifact_view(db, artifact)["stale"] is False
    assert artifact.revision_id == storage[2].current_revision_id
    assert not complete(storage) and len(calls) == 1


def test_owner_isolation_and_request_idempotency(storage):
    db, _, source, _, _, _, principal, other = storage
    key = uuid4()
    first = create(storage, key=key)
    assert create(storage, key=key)["id"] == first["id"]
    with pytest.raises(ApiError) as error:
        create(storage, kind="analyze", key=key)
    assert error.value.status_code == 409
    with pytest.raises(ApiError) as error:
        ai.create_job(ai.JobRequest(source_id=source.id, kind="analyze", request_key=uuid4()), other, db)
    assert error.value.status_code == 404
    complete(storage)
    artifact = db.scalar(select(AIArtifact))
    with pytest.raises(ApiError):
        ai.get_artifact(artifact.id, other, db)
    assert ai.list_jobs(None, 50, other, db) == {"items": []}
    assert ai.list_artifacts(None, 50, other, db) == {"items": []}


def test_apply_single_suggestion_is_idempotent_without_overwriting_new_edit(storage):
    create(storage); complete(storage)
    db, _, source, _, domain, project, principal, _ = storage
    artifact = db.scalar(select(AIArtifact))
    applied = ai.apply_artifact(artifact.id, principal, db)
    assert applied["applied_at"] and source.domain_id == domain.id
    assert list(db.scalars(select(SourceProject.project_id))) == [project.id]
    source.title = "Later manual edit"
    db.execute(delete(SourceProject)); db.commit()
    assert ai.apply_artifact(artifact.id, principal, db)["applied_at"].replace(tzinfo=None) == applied["applied_at"].replace(tzinfo=None)
    assert source.title == "Later manual edit" and not list(db.scalars(select(SourceProject.project_id)))


@pytest.mark.parametrize("change", ["title", "revision", "context"])
def test_result_keeps_frozen_evidence_and_rejects_stale_apply(storage, change):
    create(storage)
    db, _, source, chunk, _, _, principal, _ = storage
    if change == "title":
        source.title = "Edited after request"
    elif change == "revision":
        revision = SourceRevision(source_id=source.id, version=2, extracted_text="New evidence")
        db.add(revision); db.flush(); source.current_revision_id = revision.id
    else:
        db.add(PersonalContext(source_id=source.id, notes="User added personal context"))
    db.commit(); complete(storage)
    artifact = db.scalar(select(AIArtifact))
    assert ai.artifact_view(db, artifact)["stale"] is True
    assert ai.artifact_view(db, artifact)["evidence"][0]["id"] == chunk.id
    with pytest.raises(ApiError) as error:
        ai.apply_artifact(artifact.id, principal, db)
    assert error.value.code == "ai_artifact_stale"


@pytest.mark.parametrize("bad", ["citation", "domain", "project", "extra", "empty_citations", "oversize"])
def test_malicious_or_invalid_response_does_not_publish(storage, bad):
    created = create(storage)
    body = response(storage)
    if bad == "citation": body["result"]["summary_citations"] = ["foreign-evidence"]
    elif bad == "empty_citations": body["result"]["summary_citations"] = []
    elif bad == "domain": body["result"]["organizing_suggestion"]["domain_id"] = "foreign-domain"
    elif bad == "project": body["result"]["organizing_suggestion"]["project_ids"] = ["foreign-project"]
    elif bad == "extra": body["secret"] = "untrusted"
    handler = lambda _: httpx.Response(200, content=b"x" * (worker.MAX_RESPONSE_BYTES + 1)) if bad == "oversize" else httpx.Response(200, json=body)
    complete(storage, handler)
    assert storage[0].get(AIJob, created["id"]).status == "failed"
    assert storage[0].scalar(select(AIArtifact)) is None


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503, 302])
def test_http_rejection_or_unknown_has_no_retry_or_sensitive_error(storage, status):
    created = create(storage)
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, text="provider error: synthetic-secret private-evidence")
    complete(storage, handle)
    job = storage[0].get(AIJob, created["id"])
    assert job.status == ("failed" if 400 <= status < 500 else "unknown")
    assert "synthetic-secret" not in job.last_error and "private-evidence" not in job.last_error
    assert not complete(storage, handle) and len(calls) == 1


def test_timeout_result_unknown_and_not_replayed(storage):
    created = create(storage)
    def handle(request):
        raise httpx.ReadTimeout("private request synthetic-secret", request=request)
    complete(storage, handle)
    assert storage[0].get(AIJob, created["id"]).status == "unknown"
    assert not complete(storage, handle)


@pytest.mark.parametrize("purge", [False, True])
def test_deleted_during_remote_call_never_publishes(storage, purge):
    created = create(storage)
    db, sessions, source, _, _, _, principal, _ = storage
    def handle(_):
        with sessions() as second:
            if purge:
                second.execute(delete(AIJob).where(AIJob.id == created["id"]))
            else:
                second.get(Source, source.id).deleted_at = utcnow()
            second.commit()
        return httpx.Response(200, json=response(storage))
    complete(storage, handle)
    assert db.scalar(select(AIArtifact)) is None
    if not purge:
        assert db.get(AIJob, created["id"]).status == "cancelled"
    with pytest.raises(ApiError):
        if not purge: ai.list_jobs(source.id, 50, principal, db)
        else: ai.get_artifact("missing", principal, db)


def test_insecure_gateway_rejected_without_remote_request(storage, monkeypatch):
    created = create(storage)
    unsafe = replace(ai.settings, ai_gateway_url="http://example.test")
    monkeypatch.setattr(ai, "settings", unsafe); monkeypatch.setattr(worker, "settings", unsafe)
    def handle(_):
        pytest.fail("Insecure gateway must not be called")
    complete(storage, handle)
    assert storage[0].get(AIJob, created["id"]).status == "failed"


@pytest.mark.parametrize("url", ["https://", "https://host:invalid", "https://user:password@host", "https://host?q=secret", "http://192.168.1.10"])
def test_invalid_gateway_config_is_not_accepted(storage, url):
    assert not ai.gateway_configured(replace(ai.settings, ai_gateway_url=url))


def test_deleted_before_request_is_cancelled_without_remote_call(storage):
    created = create(storage)
    db, _, source, *_ = storage
    source.deleted_at = utcnow(); db.commit()
    def handle(_):
        pytest.fail("Deleted source must not be submitted")
    complete(storage, handle)
    assert db.get(AIJob, created["id"]).status == "cancelled"


def test_changed_candidate_rejects_apply(storage):
    create(storage); complete(storage)
    db, _, _, _, domain, _, principal, _ = storage
    artifact = db.scalar(select(AIArtifact))
    db.delete(domain); db.commit()
    with pytest.raises(ApiError) as error:
        ai.apply_artifact(artifact.id, principal, db)
    assert error.value.code == "ai_candidate_changed"


def test_snapshot_truncation_warning_is_persisted(storage):
    db, _, source, _, _, _, _, _ = storage
    db.add(EvidenceChunk(revision_id=source.current_revision_id, ordinal=1, text="x" * 200000, locator_end=200000))
    db.commit()
    plan = ai.preview_plan(ai.PlanRequest(source_id=source.id, kind="analyze"), storage[6], db)
    created = ai.create_job(ai.JobRequest(source_id=source.id, kind="analyze", request_key=uuid4(), plan_hash=plan["plan_hash"]), storage[6], db)
    calls = []
    def handle(request):
        payload = json.loads(request.content)
        assert sum(len(item["text"]) for item in payload["evidence"]) <= 48000
        calls.append(request)
        evidence = payload["evidence"][0]["id"]
        body = response(storage)
        body["result"].update(summary_citations=[evidence], overview={"text": "Synthetic overview", "citations": [evidence]},
            insights=[], organizing_suggestion=None)
        return httpx.Response(200, json=body)
    for _ in range(plan["calls"]):
        complete(storage, handle)
    artifact = db.scalar(select(AIArtifact).where(AIArtifact.job_id == created["id"]))
    assert len(calls) == plan["calls"] and "分析范围" in json.loads(artifact.result_json)["warnings"][0]


def test_analysis_without_suggestion_cannot_be_applied(storage):
    create(storage, kind="analyze")
    body = response(storage); body["result"]["organizing_suggestion"] = None
    complete(storage, lambda _: httpx.Response(200, json=body))
    db = storage[0]
    artifact = db.scalar(select(AIArtifact))
    with pytest.raises(ApiError) as error:
        ai.apply_artifact(artifact.id, storage[6], db)
    assert error.value.code == "ai_no_suggestion"


def test_corrupt_persisted_artifact_cannot_bypass_frozen_candidates(storage):
    create(storage); complete(storage)
    db = storage[0]
    artifact = db.scalar(select(AIArtifact))
    new_domain = Domain(owner_id=storage[6].owner_id, name="Created after snapshot")
    db.add(new_domain); db.flush()
    body = json.loads(artifact.result_json)
    body["organizing_suggestion"]["domain_id"] = new_domain.id
    artifact.result_json = json.dumps(body); db.commit()
    with pytest.raises(ApiError) as error:
        ai.apply_artifact(artifact.id, storage[6], db)
    assert error.value.code == "ai_artifact_invalid"
    assert storage[2].domain_id != new_domain.id


def test_changed_gateway_does_not_send_previously_queued_knowledge(storage, monkeypatch):
    created = create(storage)
    job = storage[0].get(AIJob, created["id"])
    assert json.loads(job.snapshot_json)["gateway_url"] == "http://127.0.0.1:8765"
    assert "synthetic-secret" not in job.snapshot_json
    changed = replace(ai.settings, ai_gateway_url="https://new-gateway.example.test")
    monkeypatch.setattr(ai, "settings", changed); monkeypatch.setattr(worker, "settings", changed)
    def handle(_):
        pytest.fail("Queued knowledge must not be sent to a changed destination")
    complete(storage, handle)
    assert storage[0].get(AIJob, created["id"]).status == "failed"
    assert "地址已变更" in storage[0].get(AIJob, created["id"]).last_error


def test_http_routes_expose_single_suggestion_contract(client, account, monkeypatch):
    from app.database import engine
    from sqlalchemy.orm import Session
    configured = replace(ai.settings, ai_gateway_url="http://127.0.0.1:8765", ai_gateway_token="synthetic-secret")
    monkeypatch.setattr(ai, "settings", configured)
    monkeypatch.setattr(worker, "settings", configured)
    headers = account["headers"]
    capture = client.post("/v1/captures", headers={**headers, "Idempotency-Key": "ai-http"}, json={
        "capture_type": "text", "title": "HTTP Synthetic", "coverage": "full_text",
        "content": {"text": "HTTP Synthetic evidence"}, "context": {"domain_id": account["domain"]["id"]},
        "client": {"kind": "desktop", "version": "test"}})
    assert capture.status_code == 201
    source_id = capture.json()["id"]
    submitted = client.post("/v1/ai/jobs", headers=headers, json={"source_id": source_id,
        "kind": "organize", "request_key": str(uuid4())})
    assert submitted.status_code == 202, submitted.text
    def handle(request):
        evidence_id = json.loads(request.content)["evidence"][0]["id"]
        return httpx.Response(200, json={"result": {"contract_version": 2, "overview": {"text": "HTTP overview", "citations": [evidence_id]}, "summary": "HTTP summary", "summary_citations": [evidence_id],
            "organizing_suggestion": {"domain_id": None, "project_ids": [account["project"]["id"]],
                "reason": "HTTP reason", "citations": [evidence_id]}}, "provider": "synthetic",
            "model": "synthetic", "prompt_version": "v1", "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    with Session(engine) as db:
        assert worker.run_ai_once(db, httpx.MockTransport(handle))
    artifacts = client.get("/v1/ai/artifacts", headers=headers, params={"source_id": source_id}).json()["items"]
    assert len(artifacts) == 1 and "organizing_suggestion" in artifacts[0]["result"]
    applied = client.post(f'/v1/ai/artifacts/{artifacts[0]["id"]}/apply', headers=headers)
    assert applied.status_code == 200 and applied.json()["applied_at"]


def test_single_job_view_counts_one_model_call_without_step_rows(storage):
    from app.ai import job_view
    from app.models import AIJob
    from sqlalchemy import select
    create(storage, kind='analyze')
    job = storage[0].scalar(select(AIJob))
    assert job_view(storage[0], job)['total_steps'] == 1
    assert job_view(storage[0], job)['completed_steps'] == 0
    worker.run_ai_once(storage[0], httpx.MockTransport(lambda _: httpx.Response(200, json=response(storage))))
    assert job_view(storage[0], job)['completed_steps'] == 1
