"""Configuration identity across document, RAG and domain checkpoints."""
from dataclasses import replace
import json

import httpx
import pytest
from sqlalchemy import select

from app import ai, domain_builds, domain_full_analysis as full, domain_full_runtime, domain_summary_inputs
from app import rag, model_switch as switch
from app.config import DEFAULT_DATABASE_URL
from app.models import AIArtifact, AIJob, DomainSummaryArtifact, DomainSummaryJob, RAGJob, RAGTurn
from services.worker import ai_analysis, domain_build, domain_summary, rag_answer
from test_ai_analysis import storage
from test_domain_full_worker import corpus, enqueue, finish, transport
from test_model_configuration import profile
from test_rag_api import storage as rag_storage, create as create_turn, response as answer


def test_full_workflow_drains_using_original_configuration_and_keeps_binding_device_only(corpus, tmp_path, monkeypatch):
    binding = profile().public_binding()
    configured = replace(ai.settings, ai_profile=binding)
    for module in (ai, domain_builds, full, domain_full_runtime, domain_summary_inputs,
                   ai_analysis, domain_build, domain_summary):
        monkeypatch.setattr(module, 'settings', configured)
    local = replace(configured, environment='local', database_url=DEFAULT_DATABASE_URL,
        local_data_dir=str(tmp_path / 'data'), local_cache_dir=str(tmp_path / 'cache'),
        local_log_dir=str(tmp_path / 'logs'), upload_dir=tmp_path / 'data/assets')
    monkeypatch.setattr(switch, 'settings', local)
    job = enqueue(corpus)
    db, _, source = corpus[:3]
    state = switch.prepare_switch(corpus[6], db)
    assert not state['can_apply']
    calls = []
    finish(corpus, transport(calls))
    assert job.status == 'completed', job.last_error
    assert calls and any(path == '/v1/summarize-node' for path, _ in calls)
    for model in (AIJob, DomainSummaryJob):
        rows = list(db.scalars(select(model)))
        assert rows and all(json.loads(row.snapshot_json)['model_binding'] == binding for row in rows)
    for model in (AIArtifact, DomainSummaryArtifact):
        rows = list(db.scalars(select(model)))
        assert rows and all('credential_ref' not in row.snapshot_json for row in rows)
    assert switch.configuration_state(corpus[6], db)['can_apply']


@pytest.mark.parametrize('changed', [False, True])
def test_rag_freezes_profile_and_does_not_use_changed_credentials(rag_storage, monkeypatch, changed):
    configured = replace(rag.settings, ai_profile=profile().public_binding())
    for module in (rag, ai, rag_answer):
        monkeypatch.setattr(module, 'settings', configured)
    created = create_turn(rag_storage)
    db = rag_storage[0]
    job = db.scalar(select(RAGJob))
    assert json.loads(job.snapshot_json)['model_binding'] == configured.ai_profile
    if changed:
        monkeypatch.setattr(rag_answer, 'settings', replace(configured,
            ai_profile={**configured.ai_profile, 'config_revision': 2}))
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(200, json=answer(rag_storage, request))
    assert rag_answer.run_rag_once(db, httpx.MockTransport(provider))
    turn = db.get(RAGTurn, created['id'])
    assert turn.status == ('failed' if changed else 'succeeded')
    assert len(calls) == (0 if changed else 1)
    assert 'credential_ref' not in turn.result_json
