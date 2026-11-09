"""Structural input bounds and dependency-safe context selection."""
from uuid import uuid4

from app.domain_contract import DomainContextSummary, DomainEvidence
from app.domain_node_inputs import partition_document, bound_parent_contexts, PARTIAL_CONTEXT


def test_partition_preserves_all_windows_with_stable_distinct_contexts():
    source, revision, node = str(uuid4()), str(uuid4()), str(uuid4())
    entries = [DomainEvidence(id=str(uuid4()), source_id=source, revision_id=revision,
        text='字' * 1000, text_start=i * 1000, text_end=(i + 1) * 1000,
        locator_kind='paragraph', locator_start=0, locator_end=1) for i in range(18)]
    context = DomainContextSummary(node_id=node, kind='document', text='完整摘要仅用于导航。',
        source_ids=[source], citations=[e.id for e in entries])
    original = {e.id: e for e in entries}
    parts = list(partition_document(context, original))
    assert len(parts) == 2 and len({part.node_id for part, _ in parts}) == 2
    assert list(partition_document(context, original)) == parts
    assert ''.join(e.text for _, selected in parts for e in selected.values()) == '字' * 18000
    assert {key for _, selected in parts for key in selected} == set(original)
    assert all(sum(len(e.text) for e in selected.values()) <= 9000 for _, selected in parts)
    assert all(part.text == PARTIAL_CONTEXT and part.citations == list(selected) for part, selected in parts)


def test_parent_selection_keeps_source_diversity_and_removes_unsupported_summaries():
    contexts, evidence = [], {}
    for number in range(2):
        source = str(uuid4()); ids = []
        for offset in range(9):
            identifier = str(uuid4()); ids.append(identifier)
            evidence[identifier] = dict(id=identifier, source_id=source,
                text='字' * 1000, text_start=offset * 1000)
        contexts.append(DomainContextSummary(node_id=str(uuid4()), kind='topic',
            text=f'仅完整引用集合支持的合成结论{number}。', source_ids=[source], citations=ids))
    retained, selected, coverage = bound_parent_contexts(contexts, evidence)
    assert sum(len(e['text']) for e in selected.values()) == 9000
    assert {e['source_id'] for e in selected.values()} == {c.source_ids[0] for c in contexts}
    assert all(c.text == PARTIAL_CONTEXT for c in retained)
    assert {identifier for c in retained for identifier in c.citations} == set(selected)
    assert coverage['retained_evidence_count'] == 9 and not coverage['all_provided_evidence_retained']
    assert bound_parent_contexts(contexts, evidence) == (retained, selected, coverage)
    full_contexts, full_evidence, full_coverage = bound_parent_contexts(contexts, evidence,
        characters=18000, sources=20, items=80)
    assert full_contexts == contexts and full_evidence == evidence and full_coverage['all_provided_evidence_retained']
