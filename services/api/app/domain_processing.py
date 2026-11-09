"""Domain processing contracts and dispatch rules; no I/O or model calls."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .ai_contract import Identifier, StrictModel

DomainScope = Literal["app", "shareable"]
DomainPhase = Literal["not_built", "dirty", "preparing", "generating", "indexing",
                      "ready", "partial", "failed", "unknown"]
NonNegative = Annotated[int, Field(ge=0)]


class DomainProcessingPolicy(StrictModel):
    enabled: bool = False
    paused: bool = False
    automatic_updates: bool = False
    # Explicit per-update scope bounds, in addition to the cumulative budget.
    automatic_max_sources: Annotated[int, Field(ge=0, le=5000)] = 0
    automatic_max_steps: Annotated[int, Field(ge=0, le=256)] = 0
    # Budgets are explicitly granted for a processing period, not per call.
    max_calls: NonNegative = 0
    max_input_tokens: NonNegative = 0
    debounce_seconds: Annotated[int, Field(ge=0, le=3600)] = 30
    max_wait_seconds: Annotated[int, Field(ge=0, le=86400)] = 300

    @model_validator(mode="after")
    def validate_wait(self):
        if self.max_wait_seconds < self.debounce_seconds:
            raise ValueError("Maximum wait cannot be shorter than debounce")
        if self.automatic_updates and (not self.enabled or not self.automatic_max_sources or not self.automatic_max_steps):
            raise ValueError("Automatic updates require enabled processing and explicit scope bounds")
        return self


class DomainBudgetUsage(StrictModel):
    # Include reserved in-flight requests: concurrent workers cannot each spend
    # the same remainder. Persistence must reserve these atomically before send.
    calls: NonNegative = 0
    input_tokens: NonNegative = 0


class DomainPendingWork(StrictModel):
    generation: NonNegative
    phase: DomainPhase
    dirty_since: datetime | None = None
    last_changed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_times(self):
        for value in (self.dirty_since, self.last_changed_at):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Processing timestamps must include a timezone")
        if (self.dirty_since is None) != (self.last_changed_at is None):
            raise ValueError("Both change timestamps are required together")
        if self.dirty_since and self.last_changed_at < self.dirty_since:
            raise ValueError("Last change cannot precede the first dirty change")
        return self


def dispatch_reason(policy: DomainProcessingPolicy, usage: DomainBudgetUsage,
                    work: DomainPendingWork, now: datetime, *, input_tokens: int,
                    manual: bool = False, confirmed_unknown: bool = False,
                    coalesced: bool = False) -> str:
    """Return 'ready' or a blocking reason. Never sends or reserves a request.

    A manual update may bypass debounce/pause, but cannot grant a budget or
    clear an unknown paid result without explicit confirmation.
    """
    if now.utcoffset() is None:
        raise ValueError("Dispatch time must include a timezone")
    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens <= 0:
        raise ValueError("A positive conservative input-token estimate is required")
    if not policy.enabled:
        return "disabled"
    if work.phase in {"preparing", "generating", "indexing"}:
        return "running"
    if work.phase == "unknown" and not (manual and confirmed_unknown):
        return "unknown_requires_confirmation"
    if work.phase == "failed" and not manual:
        return "failed_requires_manual_retry"
    if policy.paused and not manual:
        return "paused"
    if usage.calls + 1 > policy.max_calls or usage.input_tokens + input_tokens > policy.max_input_tokens:
        return "budget_exhausted"
    if work.phase == "not_built" and not manual:
        return "first_build_requires_confirmation"
    if work.dirty_since is None and work.phase not in {"not_built", "failed", "unknown"}:
        return "no_changes"
    if work.last_changed_at and now < work.last_changed_at:
        return "clock_before_change"
    if not manual and not coalesced and work.last_changed_at:
        due = min(work.last_changed_at + timedelta(seconds=policy.debounce_seconds),
                  work.dirty_since + timedelta(seconds=policy.max_wait_seconds))
        if now < due:
            return "debouncing"
    return "ready"


class DomainSourceDependency(StrictModel):
    source_id: Identifier
    revision_id: Identifier | None
    summary_artifact_id: Identifier | None
    input_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class DomainInputManifest(StrictModel):
    contract_version: Literal[1] = 1
    owner_id: Identifier
    domain_id: Identifier
    domain_name: Annotated[str, Field(min_length=1, max_length=120)]
    scope: DomainScope
    rules_version: Identifier = "domain-input-v2"
    structure_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] = "0" * 64
    sources: list[DomainSourceDependency]

    @model_validator(mode="after")
    def unique_sources(self):
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise ValueError("Duplicate source dependency")
        return self
