"""Opt-in passage results; existing source-level API and Agent ranking stay compatible."""
from __future__ import annotations

import re
from sqlalchemy import select
from .models import EvidenceChunk, SourceRevision


class EvidencePositions:
    def __init__(self, db):
        self.db = db
        self.cache = {}

    def ranges(self, revision):
        if revision.id not in self.cache:
            rows, cursor = [], 0
            for chunk in self.db.scalars(select(EvidenceChunk).where(
                    EvidenceChunk.revision_id == revision.id).order_by(EvidenceChunk.ordinal)):
                offset = chunk.locator_start if chunk.locator_kind == 'text' else None
                if offset is None or not revision.extracted_text.startswith(chunk.text, offset):
                    offset = revision.extracted_text.find(chunk.text, cursor)
                if offset is not None and offset >= 0:
                    rows.append((offset, offset + len(chunk.text), chunk))
                    cursor = max(cursor, offset + len(chunk.text))
            self.cache[revision.id] = rows
        return self.cache[revision.id]

    def evidence(self, revision, start):
        return next((chunk for lower, upper, chunk in self.ranges(revision) if lower <= start < upper), None)

    def offset(self, revision, evidence_id):
        return next((lower for lower, _, chunk in self.ranges(revision) if chunk.id == evidence_id), None)


def passage_item(base, revision, start, end, positions, *, match_type=None):
    if not (0 <= start < end <= len(revision.extracted_text)):
        return None
    item = dict(base)
    item['match_id'] = f'{base["source_id"]}:{revision.id}:{start}:{end}'
    item['match_text'] = revision.extracted_text[start:end]
    item['text_locator'] = {'kind': 'text', 'start': start, 'end': end}
    item['snippet'] = revision.extracted_text[max(0, start - 55):min(len(revision.extracted_text), end + 70)]
    evidence = positions.evidence(revision, start)
    item['evidence_id'] = evidence.id if evidence else None
    item['locator'] = ({'kind': 'pdf_page', 'start': evidence.locator_start, 'end': evidence.locator_end}
                       if evidence and evidence.locator_kind == 'pdf_page' else item['text_locator'])
    if match_type:
        item['match_type'] = match_type
        item['match_sources'] = ['original']
        for key in ('summary_snippet', 'summary_artifact_id', 'summary_coverage'):
            item.pop(key, None)
    return item


def interleave(groups, limit):
    """Keep the existing article rank and avoid one article occupying the entire panel."""
    output = []
    for index in range(limit):
        for group in groups:
            if index < len(group):
                output.append(group[index])
                if len(output) == limit:
                    return output
    return output


def exact_passages(db, scope, request):
    from .services import exact_search
    bases = exact_search(db, scope, request.model_copy(update={'result_unit': 'source'}))
    positions = EvidencePositions(db)
    groups = []
    for base in bases:
        revision = db.scalar(select(SourceRevision).where(SourceRevision.source_id == base['source_id'],
                              SourceRevision.version == base['revision']))
        if revision is None:
            continue
        group = []
        for match in re.finditer(re.escape(request.query.strip()), revision.extracted_text, flags=re.IGNORECASE):
            item = passage_item(base, revision, *match.span(), positions, match_type='keyword')
            if item:
                group.append(item)
            if len(group) == request.limit:
                break
        groups.append(group or [base])  # A title-only match has no invented body selection.
    return interleave(groups, request.limit)


def expand_passages(db, request, bases, ranked, semantic_rows):
    from .services import _best_keyword_span, _query_tokens
    positions = EvidencePositions(db)
    groups = {base['source_id']: [] for base in bases}
    by_source = {base['source_id']: base for base in bases}
    seen = {key: set() for key in groups}
    lexical_ids = {row[3].id for row in ranked}
    semantic_first = {}
    for row in semantic_rows:
        semantic_first.setdefault(row[1].id, row)

    def add(source_id, revision, start, end, match_type=None):
        if source_id not in groups or len(groups[source_id]) >= request.limit:
            return
        key = (start, end)
        if key in seen[source_id]:
            return
        # Semantic windows that include an already precise lexical hit are the same passage.
        if match_type == 'semantic' and any(lower < end and upper > start for lower, upper in seen[source_id]):
            return
        item = passage_item(by_source[source_id], revision, start, end, positions, match_type=match_type)
        if item:
            seen[source_id].add(key); groups[source_id].append(item)

    for base in bases:
        revision = db.scalar(select(SourceRevision).where(SourceRevision.source_id == base['source_id'],
                              SourceRevision.version == base['revision']))
        if revision is None:
            continue
        matched = semantic_first.get(base['source_id']) if base['source_id'] not in lexical_ids else None
        if matched is not None:
            _, _, evidence, _, embedding = matched
            offset = positions.offset(revision, evidence.id)
            if offset is not None:
                add(base['source_id'], revision, offset + embedding.start,
                    offset + min(len(evidence.text), embedding.end))
            continue
        locator = base['locator']
        if locator['kind'] == 'text' and locator['end'] > locator['start']:
            add(base['source_id'], revision, locator['start'], locator['end'])
        else:
            offset = positions.offset(revision, base.get('evidence_id'))
            if offset is not None:
                chunk = positions.evidence(revision, offset)
                local = chunk.text.find(base['snippet'])
                if local >= 0:
                    add(base['source_id'], revision, offset + local, offset + local + len(base['snippet']))

    tokens = _query_tokens(request.query)
    for _, _, _, source, evidence, revision in ranked:
        offset = positions.offset(revision, evidence.id) if source.id in groups else None
        if offset is None:
            continue
        matches = []
        for match in re.finditer(re.escape(request.query.strip()), evidence.text, flags=re.IGNORECASE):
            matches.append(match.span())
            if len(matches) == request.limit:
                break
        if not matches:
            span = _best_keyword_span(evidence.text, request.query, tokens)
            matches = [span] if span and span[1] > span[0] else []
        for start, end in matches:
            add(source.id, revision, offset + start, offset + end, 'keyword')
    for _, source, evidence, revision, embedding in semantic_rows:
        offset = positions.offset(revision, evidence.id) if source.id in groups else None
        if offset is not None:
            add(source.id, revision, offset + embedding.start,
                offset + min(len(evidence.text), embedding.end), 'semantic')
    return interleave([groups[base['source_id']] or [base] for base in bases], request.limit)
