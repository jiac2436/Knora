"""Multi-source node summaries: bounded inputs and original-evidence citations."""
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from .ai_contract import AnalysisEvidence, AnalysisUsage, Identifier, StrictModel


class DomainEvidence(AnalysisEvidence):
    source_id: Identifier
    revision_id: Identifier
    text: Annotated[str, Field(min_length=1, max_length=3000)]


class DomainContextSummary(StrictModel):
    node_id: Identifier
    kind: Literal["document", "topic"]
    text: Annotated[str, Field(min_length=1, max_length=4000)]
    source_ids: Annotated[list[Identifier], Field(min_length=1, max_length=5000)]
    citations: Annotated[list[Identifier], Field(min_length=1, max_length=4096)]


class DomainSummaryRequest(StrictModel):
    contract_version: Literal[1, 2] = 1
    purpose: Literal['overview', 'batch'] = 'overview'
    request_id: Identifier
    node_id: Identifier
    kind: Literal["topic", "domain"]
    title: Annotated[str, Field(min_length=1, max_length=120)]
    # Hierarchical requests bound cited originals so two children always fit.
    max_cited_sources: Annotated[int, Field(ge=1, le=20)] = 20
    max_cited_evidence: Annotated[int, Field(ge=1, le=80)] = 80
    max_cited_characters: Annotated[int, Field(ge=3000, le=18000)] = 18000
    summaries: Annotated[list[DomainContextSummary], Field(min_length=1, max_length=128)]
    evidence: Annotated[list[DomainEvidence], Field(min_length=1, max_length=4096)]

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value):
        if not value.strip():
            raise ValueError("Empty node title")
        return value

    @model_validator(mode="after")
    def validate_inputs(self):
        UUID(self.request_id)
        items = {e.id: e for e in self.evidence}
        if len(items) != len(self.evidence) or any(not e.text.strip() for e in self.evidence):
            raise ValueError("Invalid node evidence")
        if len({s.node_id for s in self.summaries}) != len(self.summaries):
            raise ValueError("Duplicate context nodes")
        legacy = self.contract_version == 1
        if legacy and (self.purpose != 'overview' or len(self.summaries) > 20 or len(self.evidence) > 80
                or any(len(s.text) > 1000 or len(s.citations) > 80 or len(s.source_ids) > 20 for s in self.summaries)):
            raise ValueError('Legacy node input exceeds item budget')
        if (sum(len(e.text) for e in self.evidence) > (18000 if legacy else 400000)
                or sum(len(s.text) for s in self.summaries) > (6000 if legacy else 40000)):
            raise ValueError("Node input exceeds character budget")
        for summary in self.summaries:
            if len(set(summary.source_ids)) != len(summary.source_ids) or len(set(summary.citations)) != len(summary.citations):
                raise ValueError("Duplicate context dependency")
            if not set(summary.citations) <= set(items):
                raise ValueError("Context refers to missing original evidence")
            if {items[c].source_id for c in summary.citations} != set(summary.source_ids):
                raise ValueError("Context source and citation mismatch")
            if summary.kind == "document" and len(summary.source_ids) != 1:
                raise ValueError("Document context must bind one source")
            if legacy and self.kind == "topic" and summary.kind != "document":
                raise ValueError("Topic summaries cannot feed back into topic facts")
        if set(items) != {c for s in self.summaries for c in s.citations}:
            raise ValueError("Unbound original evidence")
        if legacy and len({e.source_id for e in self.evidence}) > 20:
            raise ValueError("Too many source dependencies")
        revisions = {}
        for evidence in self.evidence:
            if revisions.setdefault(evidence.source_id, evidence.revision_id) != evidence.revision_id:
                raise ValueError("Mixed source revisions")
        if not legacy:
            for entry in self.evidence:
                UUID(entry.id)
                UUID(entry.source_id)
                UUID(entry.revision_id)
                if len(entry.text) > 1000:
                    raise ValueError('Navigation original window exceeds local bound')
        if not legacy and summary_input_reservation(self) > SUMMARY_INPUT_BUDGET:
            raise ValueError('Summary input exceeds token budget')
        return self


class DomainSummaryPoint(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=2400)]
    citations: Annotated[list[Identifier], Field(min_length=1, max_length=10)]


class DomainSummaryResult(StrictModel):
    contract_version: Literal[1] = 1
    overview: DomainSummaryPoint
    insights: Annotated[list[DomainSummaryPoint], Field(max_length=3)] = Field(default_factory=list)
    limitations: Annotated[list[DomainSummaryPoint], Field(max_length=3)] = Field(default_factory=list)


class DomainSummaryResponse(StrictModel):
    request_id: Identifier
    result: DomainSummaryResult
    provider: Annotated[str, Field(min_length=1, max_length=40)]
    model: Annotated[str, Field(min_length=1, max_length=120)]
    prompt_version: Annotated[str, Field(min_length=1, max_length=80)]
    usage: AnalysisUsage


def validate_domain_summary(request: DomainSummaryRequest, result: DomainSummaryResult) -> DomainSummaryResult:
    points = [result.overview, *result.insights, *result.limitations]
    budget = reading_budget(request)
    if request.contract_version == 1 and any(len(p.text) > 800 for p in points):
        raise ValueError('Legacy node output exceeds field budget')
    if sum(len(p.text) for p in points) > budget:
        raise ValueError("Node output exceeds reading budget")
    seen = set()
    known = {e.id for e in request.evidence}
    for point in points:
        normalized = " ".join(point.text.split()).casefold()
        if not normalized or normalized in seen:
            raise ValueError("Empty or repeated node statement")
        seen.add(normalized)
        if len(set(point.citations)) != len(point.citations) or not set(point.citations) <= known:
            raise ValueError("Unknown or duplicate node citation")
    cited = {identifier for point in points for identifier in point.citations}
    original = {e.id: e for e in request.evidence}
    if request.contract_version == 1 and (len(cited) > request.max_cited_evidence or len({original[c].source_id for c in cited}) > request.max_cited_sources
            or sum(len(original[c].text) for c in cited) > request.max_cited_characters):
        raise ValueError("Node cited originals exceed synthesis budget")
    return result


# Conservative UTF-8 bytes upper-bound tokens without a new tokenizer runtime.
# Reserve another 8k for output and 8k safety within a 64k-or-larger model.
SUMMARY_INPUT_BUDGET = 48000
SUMMARY_WIRE_BUDGET = 2 * 1024 * 1024


def reading_budget(request):
    if request.contract_version == 2 and request.purpose == 'batch':
        return 2200
    return 500 if request.kind == 'domain' else 800


def summary_provider_input(request):
    """Cloud input contains summaries and source anchors, never original text.

    Exact original windows remain in the local snapshot for permission checks,
    source navigation and original-backed RAG. Their IDs are navigation anchors,
    not a claim that the provider read or verified those originals.
    """
    return dict(contract_version=2, node_id=request.node_id, kind=request.kind,
        purpose=request.purpose, title=request.title,
        # IDs occur once: original text, source IDs and duplicate reference
        # tables stay local. The model needs only each summary's allowed IDs.
        summaries=[dict(text=s.text, citations=s.citations) for s in request.summaries])


def summary_input_reservation(request):
    import json
    return len(json.dumps(summary_provider_input(request), ensure_ascii=False,
                          sort_keys=True, separators=(',', ':')).encode()) + 8192
