"""Valid long single-source citations enter domain processing without new AI calls."""
import json
from uuid import uuid4

import pytest

from app.domain_contract import DomainSummaryRequest
from app.domain_summary_inputs import document_input, build_node_request
from app.domain_processing import DomainSourceDependency
from app.models import AIArtifact
from test_ai_analysis import storage
from test_summary_retrieval import analyzed


def long_artifact(data, text, start=0, end=None):
    db, _, source, chunk = data[:4]
    chunk.text = text; chunk.locator_end = len(text)
    db.commit()
    artifact = analyzed(data, summary='合成资料讨论关键词研究。')
    snapshot = json.loads(artifact.snapshot_json)
    entry = next(e for e in snapshot['evidence'] if e['id'] == chunk.id)
    end = len(text) if end is None else end
    entry.update(text=text[start:end], text_start=start, text_end=end)
    artifact.snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    db.commit()
    dependency = DomainSourceDependency(source_id=source.id, revision_id=source.current_revision_id,
        summary_artifact_id=artifact.id, input_sha256='a' * 64)
    return dependency, artifact


def test_long_citation_is_partitioned_without_losing_text_or_original_offsets(storage):
    text = '合成关键词研究。' * 1065 + '结论。'
    dependency, artifact = long_artifact(storage, text)
    db, _, source, chunk = storage[:4]
    context, evidence, reason = document_input(db, dependency)
    assert reason is None
    assert context.text == '合成资料讨论关键词研究。'
    parts = list(evidence.values())
    assert len(parts) >= 9 and all(len(e.text) <= 1000 for e in parts)
    assert ''.join(e.text for e in parts) == text
    assert all(e.original_evidence_id == chunk.id for e in parts)
    assert all(chunk.text[e.text_start:e.text_end] == e.text for e in parts)
    assert context.citations == list(evidence)
    assert document_input(db, dependency)[1] == evidence
    assert db.get(AIArtifact, artifact.id).id == artifact.id
    frozen = build_node_request(db, source.owner_id, source.domain_id, 'app', uuid4())
    wire = DomainSummaryRequest.model_validate(frozen['request'])
    assert len(wire.evidence) == len(parts)
    assert frozen['coverage']['included_source_count'] == 1 and not frozen['coverage']['excluded']


def test_nonzero_start_is_retained_in_partitioned_windows(storage):
    text = '前缀。' + '资料。' * 2100 + '后缀。'
    dependency, _ = long_artifact(storage, text, start=3, end=len(text)-3)
    _, evidence, reason = document_input(storage[0], dependency)
    assert reason is None
    parts = list(evidence.values())
    assert parts[0].text_start == 3 and parts[-1].text_end == len(text)-3
    assert ''.join(e.text for e in parts) == text[3:-3]


def test_short_citation_keeps_its_existing_identity(storage):
    dependency, _ = long_artifact(storage, '合成原文。')
    _, evidence, reason = document_input(storage[0], dependency)
    assert reason is None and list(evidence) == [storage[3].id]


def test_medium_citation_splits_for_precise_parent_budget(storage):
    text = '甲' * 1708
    dependency, _ = long_artifact(storage, text)
    _, evidence, reason = document_input(storage[0], dependency)
    assert reason is None
    parts = list(evidence.values())
    assert [len(e.text) for e in parts] == [1000, 708]
    assert ''.join(e.text for e in parts) == text
    assert all(e.original_evidence_id == storage[3].id for e in parts)


def test_changed_original_is_still_rejected(storage):
    dependency, _ = long_artifact(storage, '原文。' * 2100)
    storage[3].text = '修改后的原文。'; storage[0].commit()
    context, evidence, reason = document_input(storage[0], dependency)
    assert context is None and not evidence and reason == 'evidence_not_bounded'
