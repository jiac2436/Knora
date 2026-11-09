"""Bounded original-evidence question answering contract."""

from typing import Annotated
from uuid import UUID
import re

from pydantic import Field, model_validator

from .ai_contract import AnalysisUsage, Identifier, StrictModel


class RAGEvidence(StrictModel):
    id: Identifier
    source_id: Identifier
    revision_id: Identifier
    title: Annotated[str, Field(max_length=200)]
    text: Annotated[str, Field(min_length=1, max_length=3000)]
    locator_kind: Annotated[str, Field(min_length=1, max_length=30)]
    locator_start: Annotated[int, Field(ge=0)]
    locator_end: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def validate_evidence(self):
        for value in (self.id, self.source_id, self.revision_id):
            UUID(value)
        if self.locator_end < self.locator_start or not self.text.strip():
            raise ValueError("Invalid evidence range or empty text")
        return self


class RAGRequest(StrictModel):
    request_id: Identifier
    question: Annotated[str, Field(min_length=1, max_length=2000)]
    evidence: Annotated[list[RAGEvidence], Field(min_length=1, max_length=12)]

    @model_validator(mode="after")
    def validate_snapshot(self):
        UUID(self.request_id)
        if not self.question.strip():
            raise ValueError("Empty question")
        if len({item.id for item in self.evidence}) != len(self.evidence):
            raise ValueError("Duplicate evidence identifiers")
        if sum(len(item.text) for item in self.evidence) > 18000:
            raise ValueError("Evidence exceeds character limit")
        return self


class RAGResult(StrictModel):
    answer: Annotated[str, Field(min_length=1, max_length=12000)]
    citations: Annotated[list[Identifier], Field(max_length=12)]
    insufficient_evidence: bool
    warnings: Annotated[list[Annotated[str, Field(min_length=1, max_length=1000)]], Field(max_length=10)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_answer(self):
        if not self.answer.strip():
            raise ValueError("Empty answer")
        if len(set(self.citations)) != len(self.citations):
            raise ValueError("Duplicate citations")
        if not self.insufficient_evidence and not self.citations:
            raise ValueError("Answer requires citations")
        return self


class RAGResponse(StrictModel):
    request_id: Identifier
    result: RAGResult
    provider: Annotated[str, Field(min_length=1, max_length=40)]
    model: Annotated[str, Field(min_length=1, max_length=120)]
    prompt_version: Annotated[str, Field(min_length=1, max_length=80)]
    usage: AnalysisUsage

    @model_validator(mode="after")
    def validate_request_id(self):
        UUID(self.request_id)
        return self


def validate_rag_result(request: RAGRequest, result: RAGResult) -> RAGResult:
    if not set(result.citations).issubset({item.id for item in request.evidence}):
        raise ValueError("Unknown evidence citation")
    # Check UUID markers and the abbreviated UUID form observed in real output.
    # Ordinary brackets (dates, code and arrays) are not citation markers.
    markers = re.findall(r"\[([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F-]{6,}(?:\.{3}|…))\]", result.answer)
    if not {marker.lower() for marker in markers}.issubset({item.lower() for item in result.citations}):
        raise ValueError("Unknown inline evidence citation")
    return result
