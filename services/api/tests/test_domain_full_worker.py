"""Synthetic full-domain checkpoints, isolated SQLite and no paid provider."""
from dataclasses import replace
import json
from uuid import uuid4

import httpx
import numpy as np
import pytest
from sqlalchemy import select, update

from app import domain_builds, domain_full_analysis as full, domain_full_runtime, semantic
from app import summary_index, domain_node_index
from app.domain_processing import DomainProcessingPolicy
from app.domain_runtime import authorize_processing, interrupt_domain_processing
from app.domain_summary_jobs import current_node_result
from app.local_changes import install_change_triggers
from app.models import (AIArtifact, AIJob, AIJobStep, DomainBuild, DomainFullAnalysisJob,
                        DomainProcessingPreference, DomainProcessingState, DomainSummaryJob,
                        EvidenceChunk, SemanticIndexJob, Source, SourceRevision, SummaryIndexJob,
                        SourceSearchDocument, SourceSearchTerm, TagAssignment, utcnow)
from services.worker import ai_analysis, domain_build, domain_full_analysis as coordinator, domain_summary
from test_ai_analysis import storage


@pytest.fixture
def corpus(storage, monkeypatch):
    configured = replace(full.settings, ai_gateway_url='http://127.0.0.1:8765',
                         ai_gateway_token='synthetic-secret')
    for module in (full, domain_full_runtime, domain_summary, ai_analysis, domain_builds):
        monkeypatch.setattr(module, 'settings', configured)
    monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=False))
    monkeypatch.setattr(semantic, 'embed_documents', lambda texts: [
        np.array([1.0, 0.0, 0.0], dtype=np.float32).tobytes() for _ in texts])
    with storage[1].kw['bind'].begin() as connection:
        install_change_triggers(connection)
    return storage


def add_source(data, title='Another source'):
    db, _, first = data[:3]
    source = Source(owner_id=first.owner_id, domain_id=first.domain_id, title=title,
                    kind='text', coverage='full_text')
    db.add(source); db.flush()
    revision = SourceRevision(source_id=source.id, version=1, extracted_text='知识检索方法与证据。')
    db.add(revision); db.flush()
    source.current_revision_id = revision.id
    db.add(EvidenceChunk(revision_id=revision.id, ordinal=0, text=revision.extracted_text,
                         locator_end=len(revision.extracted_text)))
    db.commit()
    return source


def enqueue(data, reanalyze=False):
    db, _, source = data[:3]
    preview = full.preview_full(db, source.owner_id, source.domain_id, reanalyze)
    assert not preview['blocked_sources']
    job = full.enqueue_full(db, source.owner_id, source.domain_id, full.FullEnqueue(
        request_id=uuid4(), preview_hash=preview['preview_hash'], confirmed=True, reanalyze=reanalyze))
    db.commit()
    return job


def transport(calls, document_failure=None, node_failure=None, callback=None):
    def answer(request):
        body = json.loads(request.content)
        calls.append((request.url.path, body))
        if callback: callback(request.url.path, body)
        failure = document_failure if request.url.path == '/v1/analyze' else node_failure
        if failure:
            return httpx.Response(failure)
        citations = [body['evidence'][0]['id']]
        if request.url.path == '/v1/analyze':
            tag = next((t for t in body['tags'] if t['name'] == '知识检索'), None)
            result = dict(contract_version=3, overview=dict(text='知识检索资料', citations=citations),
                summary='知识检索资料', summary_citations=citations, insights=[], limitations=[], warnings=[],
                tag_suggestions=[dict(name='知识检索', tag_id=tag['id'] if tag else None,
                                      dimension='topic', reason='检索主题', citations=citations)])
            if body.get('phase') == 'segment': result['tag_suggestions'] = []
        else:
            result = dict(contract_version=1, overview=dict(text='资料介绍知识检索方法。', citations=citations),
                          insights=[], limitations=[])
        response = dict(provider='synthetic', model='test', prompt_version='test',
                        usage=dict(prompt_tokens=10, completion_tokens=5), result=result)
        if request.url.path != '/v1/analyze': response['request_id'] = body['request_id']
        return httpx.Response(200, json=response)
    return httpx.MockTransport(answer)


def tick(data, wire):
    db = data[0]
    if coordinator.run_full_analysis_once(db): return True
    if ai_analysis.run_ai_once(db, wire): return True
    if domain_build.run_build_once(db): return True
    if domain_summary.run_node_once(db, wire): return True
    domain_node_index.ensure_node_index(db)
    if domain_node_index.index_node_once(db): return True
    summary_index.ensure_summary_index(db)
    if summary_index.index_summary_once(db): return True
    return semantic.index_one(db) if semantic.settings.semantic_enabled else False


def finish(data, wire, limit=200):
    for _ in range(limit):
        if not tick(data, wire): return
    pytest.fail('full workflow did not converge')


def test_whole_domain_reuses_current_and_completes_without_semantic(corpus):
    add_source(corpus)
    first = enqueue(corpus)
    calls = []
    finish(corpus, transport(calls))
    db, _, source = corpus[:3]
    db.refresh(first)
    assert first.status == first.phase == 'completed', first.last_error
    assert sum(path == '/v1/analyze' for path, _ in calls) == 2
    assert db.scalar(select(TagAssignment.tag_id).limit(1)) is not None
    view = full.full_job_view(db, first)
    assert view['analyzed_sources'] == view['completed_sources'] == 2
    assert view['indexing']['keyword'] == 'ready'
    assert view['indexing']['original'] == view['indexing']['domain'] == 'disabled'
    assert view['budget']['used_calls'] == len(calls)
    assert current_node_result(db, source.owner_id, source.domain_id, 'app')['available']
    second = enqueue(corpus)
    next_calls = []
    finish(corpus, transport(next_calls))
    assert second.status == 'completed', second.last_error
    assert full.full_job_view(db, second)['reused_sources'] == 2
    assert not any(path == '/v1/analyze' for path, _ in next_calls)


def test_full_authorization_does_not_overwrite_existing_policy_or_usage(corpus):
    db, _, source = corpus[:3]
    authorize_processing(db, source.owner_id, source.domain_id, 'app', DomainProcessingPolicy(
        enabled=True, paused=True, max_calls=7, max_input_tokens=900000), uuid4())
    state = db.get(DomainProcessingState, (source.domain_id, 'app'))
    state.used_calls, state.used_input_tokens = 2, 120000
    db.commit()
    preference = db.get(DomainProcessingPreference, (source.domain_id, 'app'))
    original = (preference.policy_json, preference.authorization_id)
    job = enqueue(corpus)
    finish(corpus, transport([]))
    assert job.status == 'completed', job.last_error
    db.refresh(preference); db.refresh(state)
    assert (preference.policy_json, preference.authorization_id) == original
    assert (state.used_calls, state.used_input_tokens) == (2, 120000)


@pytest.mark.parametrize('status', [401, 504])
def test_document_failure_or_unknown_stops_next_document(corpus, status):
    add_source(corpus)
    job = enqueue(corpus)
    calls = []
    wire = transport(calls, document_failure=status)
    finish(corpus, wire)
    assert job.status == ('unknown' if status == 504 else 'failed')
    assert len(calls) == 1 and job.build_id is None
    finish(corpus, wire)
    assert len(calls) == 1


def test_changed_input_cancels_unsent_document_checkpoint(corpus):
    job = enqueue(corpus)
    assert coordinator.run_full_analysis_once(corpus[0])
    assert coordinator.run_full_analysis_once(corpus[0])
    corpus[2].title = 'Edited after consent'
    corpus[0].commit()
    calls = []
    finish(corpus, transport(calls))
    assert job.status == 'stale' and not calls
    child = corpus[0].scalar(select(AIJob))
    assert child.status == 'cancelled'


def test_restart_does_not_replay_interrupted_document(corpus):
    db = corpus[0]
    job = enqueue(corpus)
    coordinator.run_full_analysis_once(db); coordinator.run_full_analysis_once(db)
    child = db.scalar(select(AIJob))
    child.status = 'running'; db.commit()
    db.execute(update(AIJob).where(AIJob.status == 'running').values(status='unknown'))
    coordinator.interrupt_full_analysis(db); db.commit()
    assert job.status == 'unknown'
    calls = []
    finish(corpus, transport(calls))
    assert not calls


def test_known_pending_checkpoint_survives_restart(corpus):
    db = corpus[0]
    job = enqueue(corpus)
    coordinator.run_full_analysis_once(db); coordinator.run_full_analysis_once(db)
    coordinator.interrupt_full_analysis(db); db.commit()
    calls = []
    finish(corpus, transport(calls))
    assert job.status == 'completed', job.last_error
    assert sum(path == '/v1/analyze' for path, _ in calls) == 1


def test_unknown_overview_stops_and_preserves_documents(corpus):
    job = enqueue(corpus)
    calls = []
    finish(corpus, transport(calls, node_failure=504))
    assert job.status == 'unknown'
    assert len(calls) == 2
    assert corpus[0].scalar(select(AIArtifact)) is not None
    assert full.full_job_view(corpus[0], job)['completed_sources'] == 1
    finish(corpus, transport(calls))
    assert len(calls) == 2


def test_summary_index_failure_is_partial_without_starting_overview(corpus):
    db = corpus[0]
    job = enqueue(corpus)
    coordinator.run_full_analysis_once(db); coordinator.run_full_analysis_once(db)
    assert ai_analysis.run_ai_once(db, transport([]))
    summary = db.get(SummaryIndexJob, corpus[2].id)
    summary.status, summary.last_error = 'failed', 'SyntheticIndexFailure'
    db.commit()
    assert coordinator.run_full_analysis_once(db)
    assert job.status == 'partial' and job.build_id is None


def test_semantic_enabled_waits_for_original_summary_and_node_vectors(corpus, monkeypatch):
    monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=True))
    job = enqueue(corpus)
    calls = []
    finish(corpus, transport(calls))
    assert job.status == 'completed', job.last_error
    indexes = full.full_job_view(corpus[0], job)['indexing']
    assert all(indexes[name] == 'ready' for name in ('original', 'summary', 'domain'))
    assert indexes['topics'] == 'not_requested'
    assert indexes['semantic_enabled']


def test_cancel_pending_stops_without_new_model_call(corpus):
    db = corpus[0]
    job = enqueue(corpus)
    coordinator.run_full_analysis_once(db); coordinator.run_full_analysis_once(db)
    view = full.cancel_endpoint(job.domain_id, job.id, full.FullSelection(), corpus[6], db)
    assert view['status'] == 'cancelled'
    calls = []
    finish(corpus, transport(calls))
    assert not calls


def test_node_budget_reserved_before_send_and_never_uses_prior_policy(corpus):
    db, sessions, source = corpus[:3]
    job = enqueue(corpus)
    checked = []
    def check(path, body):
        if path != '/v1/summarize-node': return
        with sessions() as other:
            saved = other.get(DomainFullAnalysisJob, job.id)
            budget = json.loads(saved.budget_json)
            state = other.get(DomainProcessingState, (source.domain_id, 'app'))
            assert budget['used_calls'] >= 2
            assert state.request_id == body['request_id'] and state.used_calls == 0
            assert other.get(DomainSummaryJob, body['request_id']).status == 'running'
            checked.append(True)
    finish(corpus, transport([], callback=check))
    assert job.status == 'completed' and checked


def test_queued_document_does_not_send_after_another_source_edit(corpus):
    changed = add_source(corpus)
    db = corpus[0]
    job = enqueue(corpus)
    coordinator.run_full_analysis_once(db); coordinator.run_full_analysis_once(db)
    child = db.scalar(select(AIJob))
    other = changed if child.source_id != changed.id else corpus[2]
    other.title = 'Changed scope while waiting'
    db.commit()
    calls = []
    assert ai_analysis.run_ai_once(db, transport(calls))
    assert not calls and child.status == 'cancelled'
    assert coordinator.run_full_analysis_once(db)
    assert job.status == 'stale'


def test_cancel_inflight_document_does_not_continue_or_hide_unknown(corpus):
    add_source(corpus)
    db, sessions = corpus[:2]
    job = enqueue(corpus)
    calls = []
    def cancel(path, body):
        if path != '/v1/analyze': return
        with sessions() as other:
            full.cancel_endpoint(job.domain_id, job.id, full.FullSelection(), corpus[6], other)
    finish(corpus, transport(calls, callback=cancel))
    db.refresh(job)
    assert job.status == 'unknown'
    assert len(calls) == 1 and job.build_id is None
    assert db.scalar(select(AIArtifact)) is None


def test_semantic_failure_finishes_partial_without_fake_index_success(corpus, monkeypatch):
    monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=True))
    db = corpus[0]
    job = enqueue(corpus)
    calls = []
    wire = transport(calls)
    for _ in range(100):
        tick(corpus, wire)
        if job.phase == 'indexing': break
    else:
        pytest.fail('overview did not finish')
    raw = db.get(SemanticIndexJob, corpus[2].id)
    if raw is None:
        raw = SemanticIndexJob(source_id=corpus[2].id, revision_id=corpus[2].current_revision_id)
        db.add(raw)
    raw.status, raw.last_error = 'failed', 'SyntheticModelUnavailable'
    db.commit()
    assert coordinator.run_full_analysis_once(db)
    assert job.status == 'partial'
    assert full.full_job_view(db, job)['indexing']['original'] == 'failed'
    assert current_node_result(db, job.owner_id, job.domain_id, 'app')['available']


def test_node_unknown_after_restart_does_not_replay(corpus):
    db = corpus[0]
    job = enqueue(corpus)
    calls = []
    wire = transport(calls)
    for _ in range(100):
        if coordinator.run_full_analysis_once(db): continue
        if ai_analysis.run_ai_once(db, wire): continue
        summary_index.ensure_summary_index(db)
        if summary_index.index_summary_once(db): continue
        if domain_build.run_build_once(db): break
    node = db.scalar(select(DomainSummaryJob).where(DomainSummaryJob.status == 'pending'))
    assert node is not None
    node.status = 'running'; db.commit()
    interrupt_domain_processing(db)
    domain_summary.interrupt_node_jobs(db)
    domain_build.interrupt_builds(db)
    coordinator.interrupt_full_analysis(db); db.commit()
    assert job.status == 'unknown'
    before = len(calls)
    finish(corpus, wire)
    assert len(calls) == before


def test_existing_valid_v2_analysis_reuses_without_forced_paid_upgrade(corpus):
    from test_summary_retrieval import analyzed
    analyzed(corpus)
    job = enqueue(corpus)
    calls = []
    finish(corpus, transport(calls))
    assert job.status == 'completed', job.last_error
    assert full.full_job_view(corpus[0], job)['reused_sources'] == 1
    assert all(path != '/v1/analyze' for path, _ in calls)


def test_long_document_known_checkpoint_resumes_without_replaying_segment(corpus):
    db, _, source = corpus[:3]
    revision = db.get(SourceRevision, source.current_revision_id)
    revision.extracted_text = '知识检索方法和证据。' * 6000
    evidence = db.get(EvidenceChunk, corpus[3].id)
    evidence.text, evidence.locator_end = revision.extracted_text, len(revision.extracted_text)
    db.commit()
    job = enqueue(corpus)
    coordinator.run_full_analysis_once(db); coordinator.run_full_analysis_once(db)
    child = db.scalar(select(AIJob))
    calls = []
    wire = transport(calls)
    assert ai_analysis.run_ai_once(db, wire)
    steps = list(db.scalars(select(AIJobStep).where(AIJobStep.job_id == child.id)))
    assert any(step.status == 'completed' for step in steps)
    child.status = 'unknown'; db.commit()
    coordinator.interrupt_full_analysis(db); db.commit()
    assert child.status == 'pending' and job.status == 'running'
    finish(corpus, wire)
    assert job.status == 'completed', job.last_error
    request_ids = [body['request_id'] for path, body in calls if path == '/v1/analyze']
    assert len(request_ids) == len(set(request_ids))


def test_changed_scope_after_node_claim_never_publishes_stale_overview(corpus):
    db = corpus[0]
    job = enqueue(corpus)
    calls = []
    def edit(path, _):
        if path == '/v1/summarize-node':
            corpus[2].title = 'Changed during paid node call'
            db.commit()
    finish(corpus, transport(calls, callback=edit))
    assert job.status == 'stale'
    result = current_node_result(db, job.owner_id, job.domain_id, 'app')
    assert not result['available']


def test_budget_exhaustion_fences_node_before_http(corpus):
    db = corpus[0]
    job = enqueue(corpus)
    calls = []
    wire = transport(calls)
    for _ in range(100):
        if coordinator.run_full_analysis_once(db): continue
        if ai_analysis.run_ai_once(db, wire): continue
        summary_index.ensure_summary_index(db)
        if summary_index.index_summary_once(db): continue
        if domain_build.run_build_once(db): break
    budget = json.loads(job.budget_json)
    budget['used_calls'] = budget['calls']
    job.budget_json = full.encode(budget)
    db.commit()
    before = len(calls)
    assert domain_summary.run_node_once(db, wire)
    finish(corpus, wire)
    assert job.status == 'cancelled' and 'budget_exhausted' in job.last_error
    assert len(calls) == before


@pytest.mark.parametrize('action', ['cancel_queued', 'stale_queued', 'stale_after_segment'])
def test_stop_long_document_preserves_valid_checkpoint_states(corpus, action):
    db, _, source = corpus[:3]
    revision = db.get(SourceRevision, source.current_revision_id)
    revision.extracted_text = '知识检索方法和证据。' * 6000
    evidence = db.get(EvidenceChunk, corpus[3].id)
    evidence.text, evidence.locator_end = revision.extracted_text, len(revision.extracted_text)
    db.commit()
    job = enqueue(corpus)
    coordinator.run_full_analysis_once(db); coordinator.run_full_analysis_once(db)
    child = db.scalar(select(AIJob))
    calls = []
    wire = transport(calls)
    if action == 'stale_after_segment':
        assert ai_analysis.run_ai_once(db, wire)
    before = len(calls)
    if action == 'cancel_queued':
        full.cancel_endpoint(job.domain_id, job.id, full.FullSelection(), corpus[6], db)
    else:
        source.title = 'User edit stops subsequent sections'
        db.commit()
        assert coordinator.run_full_analysis_once(db)
    db.refresh(child)
    assert child.status == 'cancelled'
    assert job.status == ('cancelled' if action == 'cancel_queued' else 'stale')
    states = list(db.scalars(select(AIJobStep.status).where(AIJobStep.job_id == child.id)))
    assert states and set(states) <= {'pending', 'completed'}
    if action == 'stale_after_segment': assert 'completed' in states
    finish(corpus, wire)
    assert len(calls) == before


def test_real_worker_loop_hooks_complete_without_starving_local_indexes(corpus, monkeypatch):
    from services.worker import main
    add_source(corpus)
    job = enqueue(corpus)
    calls = []
    wire = transport(calls)
    monkeypatch.setattr(main, 'run_ai_once', lambda db: ai_analysis.run_ai_once(db, wire))
    monkeypatch.setattr(main, 'run_node_once', lambda db: domain_summary.run_node_once(db, wire))
    for _ in range(200):
        main.run_once(corpus[0])
        corpus[0].refresh(job)
        if job.status not in ('pending', 'running'): break
    assert job.status == 'completed', job.last_error
    assert full.full_job_view(corpus[0], job)['completed_sources'] == 2
    assert sum(path == '/v1/analyze' for path, _ in calls) == 2


def test_final_indexing_rebuilds_stale_keyword_marker_once(corpus, monkeypatch):
    from app import services
    monkeypatch.setattr(semantic, 'settings', replace(semantic.settings, semantic_enabled=True))
    db, _, source = corpus[:3]
    document = SourceSearchDocument(source_id=source.id, owner_id=source.owner_id)
    db.add(document); db.flush()
    db.add_all([SourceSearchTerm(document_id=document.id, term='__indexed__'),
                SourceSearchTerm(document_id=document.id, term='obsolete-cache')])
    db.commit()
    job = enqueue(corpus)
    calls = []
    wire = transport(calls)
    for _ in range(100):
        tick(corpus, wire)
        if job.phase == 'indexing': break
    else:
        pytest.fail('overview did not finish')
    rebuilt = []
    original = services.reindex_source
    def reindex(session, selected):
        rebuilt.append(selected.id)
        return original(session, selected)
    monkeypatch.setattr(services, 'reindex_source', reindex)
    finish(corpus, wire)
    assert job.status == 'completed', job.last_error
    assert rebuilt == [source.id]
    terms = set(db.scalars(select(SourceSearchTerm.term).where(SourceSearchTerm.document_id == document.id)))
    assert 'obsolete-cache' not in terms and 'synthetic' in terms
    assert json.loads(job.progress_json)['keyword_sources'] == [source.id]


def test_checked_mode_reanalyzes_even_current_artifacts(corpus):
    add_source(corpus)
    first = enqueue(corpus)
    finish(corpus, transport([]))
    previous = {a.id for a in corpus[0].scalars(select(AIArtifact))}
    second = enqueue(corpus, reanalyze=True)
    calls = []
    finish(corpus, transport(calls))
    assert second.status == 'completed', second.last_error
    view = full.full_job_view(corpus[0], second)
    assert view['reanalyze'] and view['analyzed_sources'] == 2 and view['reused_sources'] == 0
    assert sum(p == '/v1/analyze' for p, _ in calls) == 2
    assert len(set(corpus[0].scalars(select(AIArtifact.id))) - previous) == 2
    assert view['token_usage'] == dict(input_tokens=len(calls)*10, output_tokens=len(calls)*5, total_tokens=len(calls)*15)


def test_default_skips_stale_and_overview_never_sends_it(corpus):
    stale = add_source(corpus, 'Will become stale')
    first = enqueue(corpus)
    finish(corpus, transport([]))
    stale.title = 'Changed after analysis'
    corpus[0].commit()
    fresh = add_source(corpus, 'Never analyzed')
    second = enqueue(corpus)
    calls = []
    finish(corpus, transport(calls))
    assert second.status == 'completed', second.last_error
    view = full.full_job_view(corpus[0], second)
    assert view['stale_sources'] == view['skipped_sources'] == 1
    assert view['analyzed_sources'] == 1 and view['reused_sources'] == 1
    assert view['completed_sources'] == 3
    assert [b['source_id'] for p, b in calls if p == '/v1/analyze'] == [fresh.id]
    for path, body in calls:
        if path != '/v1/analyze':
            assert all(stale.id not in summary['source_ids'] for summary in body['summaries'])
            assert all(e['source_id'] != stale.id for e in body['evidence'])
    assert stale.id in view['overview_coverage']['excluded']
    assert current_node_result(corpus[0], stale.owner_id, stale.domain_id, 'app')['available']
    assert view['token_usage']['total_tokens'] == len(calls)*15


def test_receipts_exclude_reuse_and_hide_incomplete_usage(corpus):
    first = enqueue(corpus)
    assert full.full_job_view(corpus[0], first)['token_usage'] is None
    calls = []
    finish(corpus, transport(calls))
    assert full.full_job_view(corpus[0], first)['token_usage']['total_tokens'] == len(calls)*15
    second = enqueue(corpus)
    next_calls = []
    finish(corpus, transport(next_calls))
    assert full.full_job_view(corpus[0], second)['token_usage']['total_tokens'] == len(next_calls)*15
    artifact = corpus[0].scalar(select(AIArtifact))
    artifact.usage_json = '{}'
    corpus[0].commit()
    assert full.full_job_view(corpus[0], first)['token_usage'] is None
    # No new calls in second means zero, even if old receipts are unavailable.
    assert full.full_job_view(corpus[0], second)['token_usage']['total_tokens'] == 0


def test_long_document_receipts_count_final_aggregate_once(corpus):
    db, _, source = corpus[:3]
    revision = db.get(SourceRevision, source.current_revision_id)
    evidence = db.scalar(select(EvidenceChunk).where(EvidenceChunk.revision_id == revision.id))
    revision.extracted_text = evidence.text = '知识检索方法与证据。' * 7000
    evidence.locator_end = len(evidence.text)
    db.commit()
    job = enqueue(corpus)
    calls = []
    finish(corpus, transport(calls))
    assert job.status == 'completed', job.last_error
    assert sum(p == '/v1/analyze' for p, _ in calls) > 1
    assert full.full_job_view(db, job)['token_usage']['total_tokens'] == len(calls)*15
