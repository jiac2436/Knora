"""Bounded wire contract shared by the local worker and AI gateway."""

import unicodedata
import re
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


Identifier = Annotated[str, Field(min_length=1, max_length=128)]
CitationList = Annotated[list[Identifier], Field(max_length=80)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AnalysisEvidence(StrictModel):
    id: Identifier
    image_id: Identifier | None = None
    original_evidence_id: Identifier | None = None
    text_start: Annotated[int, Field(ge=0)] = 0
    text_end: Annotated[int, Field(ge=0)] | None = None
    text: Annotated[str, Field(min_length=1, max_length=48000)]
    locator_kind: Annotated[str, Field(min_length=1, max_length=30)]
    locator_start: Annotated[int, Field(ge=0)]
    locator_end: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_range(self):
        if self.locator_end < self.locator_start:
            raise ValueError("Invalid evidence locator range")
        if self.text_end is not None and self.text_end - self.text_start != len(self.text):
            raise ValueError("Evidence text offset range does not match its text")
        return self


class AnalysisCandidate(StrictModel):
    id: Identifier
    name: Annotated[str, Field(min_length=1, max_length=200)]


TagDimension = Literal["topic", "type", "entity"]
TagName = Annotated[str, Field(min_length=1, max_length=120)]


def normalize_tag_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).split()).casefold()


class AnalysisTag(StrictModel):
    id: Identifier
    name: TagName
    dimension: TagDimension
    aliases: Annotated[list[TagName], Field(max_length=100)] = Field(default_factory=list)


class RejectedTag(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=160)]
    dimension: TagDimension


SectionCitations = Annotated[list[Identifier], Field(min_length=1, max_length=5)]


class SectionInsight(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=400)]
    citations: SectionCitations


class SectionOverview(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=300)]
    citations: SectionCitations


class SectionSummary(StrictModel):
    section_id: Annotated[int, Field(ge=1, le=8)]
    overview: SectionOverview
    summary: Annotated[str, Field(min_length=1, max_length=2000)]
    summary_citations: SectionCitations
    insights: Annotated[list[SectionInsight], Field(max_length=5)] = Field(default_factory=list)
    limitations: Annotated[list[SectionInsight], Field(max_length=3)] = Field(default_factory=list)

    def citation_groups(self) -> list[list[str]]:
        return [self.overview.citations, self.summary_citations,
                *(item.citations for item in self.insights),
                *(item.citations for item in self.limitations)]

    @model_validator(mode="after")
    def validate_references(self):
        groups = self.citation_groups()
        if any(len(set(refs)) != len(refs) for refs in groups):
            raise ValueError("Duplicate section evidence reference")
        if len({identifier for refs in groups for identifier in refs}) > 10:
            raise ValueError("Section has too many distinct evidence references")
        return self


class AnalysisImage(StrictModel):
    id: Identifier
    evidence_id: Identifier
    ordinal: Annotated[int, Field(ge=0)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    mime_type: Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
    size_bytes: Annotated[int, Field(gt=0, le=20 * 1024 * 1024)]
    width: Annotated[int, Field(gt=0, le=30_000_000)]
    height: Annotated[int, Field(gt=0, le=30_000_000)]
    # Binary is hydrated for HTTP only, never persisted in a job or artifact.
    data_url: Annotated[str, Field(max_length=28 * 1024 * 1024)] | None = None


class AnalysisRequest(StrictModel):
    contract_version: Annotated[int, Field(ge=1, le=3)] = 1
    phase: Literal["single", "segment", "synthesis"] = "single"
    section_summaries: Annotated[list[SectionSummary], Field(max_length=8)] = Field(default_factory=list)
    request_id: Identifier
    kind: Literal["analyze", "organize"]
    source_id: Identifier
    revision_id: Identifier
    evidence: Annotated[list[AnalysisEvidence], Field(min_length=1, max_length=80)]
    images: Annotated[list[AnalysisImage], Field(max_length=200)] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    domains: Annotated[list[AnalysisCandidate], Field(max_length=200)] = Field(default_factory=list)
    projects: Annotated[list[AnalysisCandidate], Field(max_length=200)] = Field(default_factory=list)

    tags: Annotated[list[AnalysisTag], Field(max_length=500)] = Field(default_factory=list)
    rejected_tags: Annotated[list[RejectedTag], Field(max_length=500)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_snapshot(self):
        UUID(self.request_id)
        if self.images:
            if self.contract_version != 3 or self.kind != "analyze":
                raise ValueError("Images require unified analysis version 3")
            if len({image.id for image in self.images}) != len(self.images):
                raise ValueError("Duplicate analysis images")
            if any(image.evidence_id not in {e.id for e in self.evidence} for image in self.images):
                raise ValueError("Image is not bound to provided evidence")
            by_id = {e.id: e for e in self.evidence}
            if any(by_id[image.evidence_id].image_id != image.id for image in self.images):
                raise ValueError("Visual citation must identify its original image")
        if any(e.image_id and not any(image.id == e.image_id and image.evidence_id == e.id
                for image in self.images) for e in self.evidence):
            raise ValueError("Visual evidence has no image binding")
        if self.images:
            if self.phase == "synthesis" and any(image.data_url for image in self.images):
                raise ValueError("Synthesis uses previously reviewed image checkpoints")
        if self.phase != "single" and self.contract_version < 2:
            raise ValueError("Long analysis phases require version 2 or later")
        if self.contract_version == 3 and self.kind != "analyze":
            raise ValueError("Version 3 only supports knowledge analysis")
        if self.phase != "synthesis" and self.section_summaries:
            raise ValueError("Section summaries are only allowed for synthesis")
        if self.phase == "synthesis":
            if not self.section_summaries:
                raise ValueError("Synthesis requires section summaries")
            if len({item.section_id for item in self.section_summaries}) != len(self.section_summaries):
                raise ValueError("Duplicate section identifiers")
            evidence_ids = {item.id for item in self.evidence}
            used_ids = set()
            for section in self.section_summaries:
                for refs in section.citation_groups():
                    if not refs or len(set(refs)) != len(refs) or not set(refs).issubset(evidence_ids):
                        raise ValueError("Invalid section evidence reference")
                    used_ids.update(refs)
            if used_ids != evidence_ids:
                raise ValueError("Synthesis evidence must be cited by section summaries")
        if sum(len(item.text) for item in self.evidence) > 48000:
            raise ValueError("Evidence exceeds character limit")
        for items in (self.evidence, self.domains, self.projects, self.tags):
            if len({item.id for item in items}) != len(items):
                raise ValueError("Duplicate snapshot identifiers")
        names = [(tag.dimension, normalize_tag_name(tag.name)) for tag in self.tags]
        if any(not name for _, name in names) or len(set(names)) != len(names):
            raise ValueError("Empty or duplicate normalized tag names")
        return self


class AnalysisInsight(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=4000)]
    citations: CitationList


class OrganizingSuggestion(StrictModel):
    domain_id: Identifier | None
    project_ids: Annotated[list[Identifier], Field(max_length=200)]
    reason: Annotated[str, Field(min_length=1, max_length=4000)]
    citations: CitationList


class TagSuggestion(StrictModel):
    tag_id: Identifier | None
    name: TagName
    dimension: TagDimension
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    citations: CitationList

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        display = " ".join(unicodedata.normalize("NFKC", value).split())
        key = display.casefold()
        if not display or len(display) > 120 or len(key) > 160 or any(unicodedata.category(c).startswith("C") for c in display):
            raise ValueError("Invalid tag name")
        return value


class AnalysisResult(StrictModel):
    contract_version: Annotated[int, Field(ge=1, le=3)] = 1
    overview: AnalysisInsight | None = None
    limitations: Annotated[list[AnalysisInsight], Field(max_length=10)] = Field(default_factory=list)
    tag_suggestions: Annotated[list[TagSuggestion], Field(max_length=8)] = Field(default_factory=list)
    summary: Annotated[str, Field(min_length=1, max_length=12000)]
    summary_citations: CitationList
    insights: Annotated[list[AnalysisInsight], Field(max_length=20)] = Field(default_factory=list)
    organizing_suggestion: OrganizingSuggestion | None = None
    warnings: Annotated[list[Annotated[str, Field(min_length=1, max_length=1000)]], Field(max_length=20)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_overview(self):
        if self.overview is not None and len(self.overview.text) > 300:
            raise ValueError("Overview exceeds character limit")
        return self


class AnalysisUsage(StrictModel):
    prompt_tokens: Annotated[int, Field(ge=0)]
    completion_tokens: Annotated[int, Field(ge=0)]


class GatewayResponse(StrictModel):
    result: AnalysisResult
    provider: Annotated[str, Field(min_length=1, max_length=40)]
    model: Annotated[str, Field(min_length=1, max_length=200)]
    prompt_version: Annotated[str, Field(min_length=1, max_length=80)]
    usage: AnalysisUsage


def validate_result(request: AnalysisRequest, result: AnalysisResult) -> AnalysisResult:
    """Validate reference integrity; semantic accuracy still needs evaluation."""
    if result.contract_version != request.contract_version:
        raise ValueError("Analysis contract version mismatch")
    if request.phase == "segment":
        if result.tag_suggestions or result.organizing_suggestion is not None:
            raise ValueError("Segments must not propose tags or organization")
        SectionSummary.model_validate({
            "section_id": 1, "overview": result.overview.model_dump() if result.overview else None,
            "summary": result.summary, "summary_citations": result.summary_citations,
            "insights": [item.model_dump() for item in result.insights],
            "limitations": [item.model_dump() for item in result.limitations],
        })
    if request.contract_version >= 2 and result.overview is None:
        raise ValueError("Version 2 or later requires a grounded overview")
    if request.contract_version == 3 and request.phase != "segment":
        validate_compact_result(request, result)
    evidence_ids = {item.id for item in request.evidence}
    citations = [result.summary_citations, *(item.citations for item in result.insights)]
    if result.overview is not None:
        citations.append(result.overview.citations)
    citations.extend(item.citations for item in result.limitations)
    known_tags = {item.id: item for item in request.tags}
    rejected = {(item.dimension, normalize_tag_name(item.name)) for item in request.rejected_tags}
    seen_names, seen_ids = set(), set()
    for tag in result.tag_suggestions:
        name = normalize_tag_name(tag.name)
        key = (tag.dimension, name)
        if not name or key in seen_names or key in rejected:
            raise ValueError("Empty, duplicate or rejected tag suggestion")
        seen_names.add(key)
        if tag.tag_id is not None:
            existing = known_tags.get(tag.tag_id)
            if existing is None or tag.tag_id in seen_ids:
                raise ValueError("Unknown or duplicate suggested tag")
            if existing.dimension != tag.dimension or normalize_tag_name(existing.name) != name:
                raise ValueError("Suggested tag differs from catalog")
            names = [existing.name, *existing.aliases]
            if any((existing.dimension, normalize_tag_name(alias)) in rejected for alias in names):
                raise ValueError("Previously rejected suggested tag")
            seen_ids.add(tag.tag_id)
        else:
            if tag.dimension == "type":
                raise ValueError("New content type is not allowed")
            if any(existing.dimension == tag.dimension and name in {normalize_tag_name(n) for n in [existing.name, *existing.aliases]} for existing in request.tags):
                raise ValueError("Existing tag requires its catalog identifier")
        citations.append(tag.citations)
    suggestion = result.organizing_suggestion
    if suggestion is not None:
        if suggestion.domain_id is not None and suggestion.domain_id not in {item.id for item in request.domains}:
            raise ValueError("Unknown suggested domain")
        project_ids = set(suggestion.project_ids)
        if len(project_ids) != len(suggestion.project_ids) or not project_ids.issubset({item.id for item in request.projects}):
            raise ValueError("Unknown or duplicate suggested project")
        citations.append(suggestion.citations)
    for references in citations:
        if not references or len(set(references)) != len(references) or not set(references).issubset(evidence_ids):
            raise ValueError("Missing, duplicate or unknown evidence reference")
    return result


def validate_compact_result(request: AnalysisRequest, result: AnalysisResult) -> None:
    """Bound reading effort without truncating generated facts or their references."""
    if result.organizing_suggestion is not None:
        raise ValueError("Knowledge analysis must not change domain or projects")
    if len(result.tag_suggestions) > 5 or len(result.insights) > 3:
        raise ValueError("Compact analysis has too many tags or insights")
    overview = result.overview.text if result.overview else ""
    texts = [overview, *(item.text for item in result.insights)]
    chinese = bool(re.search(r"[\u3400-\u9fff]", "".join(texts) + result.summary))
    budget = 300 if chinese else 600
    original_chars = sum(len(item.text.strip()) for item in request.evidence)
    if request.phase == "single" and original_chars <= 300 and not request.images:
        budget = min(budget, original_chars)
        if len(result.insights) > 1:
            raise ValueError("Short notes must not become long reports")
    if len(overview) > (100 if chinese else 200) or sum(map(len, texts)) > budget:
        raise ValueError("Compact overview and insights exceed reading budget")
    if len(result.summary) > budget or any(len(item.text) > (120 if chinese else 240) for item in result.insights):
        raise ValueError("Compact summary or insight exceeds character budget")
    if len(result.limitations) > 1 or any(len(item.text) > 120 for item in result.limitations):
        raise ValueError("Compact analysis has too many limitations")
    normalized = [" ".join(text.split()).casefold() for text in texts]
    if len(set(normalized)) != len(normalized):
        raise ValueError("Compact analysis repeats the same statement")
    if any(len(tag.reason) > 160 for tag in result.tag_suggestions):
        raise ValueError("Tag explanations exceed compact budget")
