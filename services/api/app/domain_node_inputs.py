"""Bound offered originals before generation; never trim generated conclusions."""
from uuid import NAMESPACE_URL, uuid5


PARTIAL_CONTEXT = '仅保留该节点的部分原文引用；请据提供的原文生成概览，不沿用未核实的摘要结论。'


def partition_document(context, evidence):
    """Offer all original windows through bounded leaves, including long sources."""
    parts, current, characters = [], {}, 0
    for identifier, entry in evidence.items():
        if current and (len(current) == 40 or characters + len(entry.text) > 9000):
            parts.append(current); current, characters = {}, 0
        current[identifier] = entry; characters += len(entry.text)
    if current:
        parts.append(current)
    for part in parts:
        if len(parts) == 1:
            yield context, part
        else:
            identity = ':'.join(part)
            yield context.model_copy(update=dict(node_id=str(uuid5(NAMESPACE_URL,
                f'domain-document-part:{context.node_id}:{identity}')),
                text=PARTIAL_CONTEXT, citations=list(part))), part


def bound_parent_contexts(contexts, evidence, *, characters=9000, sources=10, items=40):
    """Select whole windows across sources; remove summaries losing references.

    Round-robin selection favors source coverage, then fills with original-order
    windows. This is navigation context selection, not proof of full-text coverage.
    """
    grouped = {}
    for identifier, entry in evidence.items():
        grouped.setdefault(entry['source_id'], []).append((identifier, entry))
    ordered = [sorted(grouped[source], key=lambda pair: (pair[1]['text_start'], pair[0]))
               for source in sorted(grouped)[:sources]]
    selected, used = {}, 0
    for offset in range(max((len(group) for group in ordered), default=0)):
        for group in ordered:
            if offset >= len(group):
                continue
            identifier, entry = group[offset]
            if len(selected) < items and used + len(entry['text']) <= characters:
                selected[identifier] = entry; used += len(entry['text'])
    retained = []
    for context in contexts:
        citations = [identifier for identifier in context.citations if identifier in selected]
        if not citations:
            continue
        retained.append(context.model_copy(update=dict(citations=citations,
            source_ids=sorted({selected[identifier]['source_id'] for identifier in citations}),
            text=context.text if len(citations) == len(context.citations) else PARTIAL_CONTEXT)))
    return retained, selected, dict(provided_evidence_count=len(evidence),
        retained_evidence_count=len(selected), provided_source_count=len(grouped),
        retained_source_count=len({e['source_id'] for e in selected.values()}),
        all_provided_evidence_retained=len(selected) == len(evidence),
        selection='source_round_robin_original_order')
