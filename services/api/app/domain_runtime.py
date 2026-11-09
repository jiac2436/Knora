"""Transactional domain policy and dispatch reservations; caller commits."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .domain_processing import (DomainBudgetUsage, DomainPendingWork, DomainProcessingPolicy,
                                DomainScope, dispatch_reason)
from .errors import ApiError
from .models import DomainTrash
from .models import Domain, DomainProcessingPreference, DomainProcessingState, DomainBuild


def locked_state(db: Session, owner_id: str, domain_id: str, scope: DomainScope) -> DomainProcessingState:
    if scope not in {"app", "shareable"}:
        raise ValueError("Unsupported domain scope")
    domain = db.scalar(select(Domain).where(Domain.id == domain_id, Domain.owner_id == owner_id))
    if domain is None or db.get(DomainTrash, domain_id) is not None:
        raise ApiError(404, "not_found", "知识域不存在")
    # Short database lock only. Never hold this transaction across model calls.
    db.execute(update(Domain).where(Domain.id == domain_id, Domain.owner_id == owner_id)
               .values(name=Domain.name))
    db.expire_all()
    state = db.get(DomainProcessingState, (domain_id, scope))
    if state is None:
        now = datetime.now(timezone.utc)
        state = DomainProcessingState(domain_id=domain_id, scope=scope, owner_id=owner_id,
            generation=1, dirty_since=now, last_changed_at=now)
        db.add(state); db.flush()
    if state.owner_id != owner_id:
        raise ApiError(404, "not_found", "知识域不存在")
    return state


def authorize_processing(db: Session, owner_id: str, domain_id: str, scope: DomainScope,
                         policy: DomainProcessingPolicy, authorization_id: UUID) -> None:
    state = locked_state(db, owner_id, domain_id, scope)
    preference = db.get(DomainProcessingPreference, (domain_id, scope))
    key = str(authorization_id)
    if preference and preference.authorization_id == key:
        if DomainProcessingPolicy.model_validate_json(preference.policy_json) != policy:
            raise ApiError(409, "conflict", "相同授权编号不能改变预算或策略")
        return
    active_build = db.scalar(select(DomainBuild.id).where(DomainBuild.domain_id == domain_id,
        DomainBuild.scope == scope, DomainBuild.status.in_(("pending", "running"))))
    if active_build or state.phase in {"preparing", "generating", "indexing", "unknown"}:
        raise ApiError(409, "conflict", "请先处理进行中或结果未知的请求")
    if preference is None:
        preference = DomainProcessingPreference(domain_id=domain_id, scope=scope, owner_id=owner_id,
            policy_json=policy.model_dump_json(), authorization_id=key)
        db.add(preference)
    else:
        preference.policy_json, preference.authorization_id = policy.model_dump_json(), key
    state.used_calls, state.used_input_tokens = 0, 0
    state.first_build_confirmed = policy.enabled
    db.flush()


def set_processing_paused(db: Session, owner_id: str, domain_id: str, scope: DomainScope, paused: bool) -> None:
    locked_state(db, owner_id, domain_id, scope)
    preference = db.get(DomainProcessingPreference, (domain_id, scope))
    if preference is None:
        raise ApiError(409, "conflict", "尚未开启知识域分析")
    policy = DomainProcessingPolicy.model_validate_json(preference.policy_json)
    policy.paused = paused
    preference.policy_json = policy.model_dump_json()
    db.flush()


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def interrupt_domain_processing(db: Session) -> None:
    """Single-worker startup: retain reservations and never replay paid work."""
    db.execute(update(DomainProcessingState).where(DomainProcessingState.phase == "generating")
               .values(phase="unknown"))


def reserve_domain_call(db: Session, owner_id: str, domain_id: str, scope: DomainScope, *,
                        generation: int, request_id: UUID, input_tokens: int, now: datetime,
                        manual: bool = False, confirmed_unknown: bool = False,
                        coalesced: bool = False) -> str:
    state = locked_state(db, owner_id, domain_id, scope)
    preference = db.get(DomainProcessingPreference, (domain_id, scope))
    if preference is None:
        return "disabled"
    if state.request_id == str(request_id):
        return "already_reserved"
    if state.generation != generation:
        return "stale_generation"
    policy = DomainProcessingPolicy.model_validate_json(preference.policy_json)
    if coalesced and not policy.automatic_updates:
        return "automatic_updates_disabled"
    phase = state.phase
    if phase == "not_built" and state.first_build_confirmed:
        phase = "dirty"
    pending = DomainPendingWork(generation=state.generation, phase=phase,
        dirty_since=_aware(state.dirty_since), last_changed_at=_aware(state.last_changed_at))
    reason = dispatch_reason(policy, DomainBudgetUsage(calls=state.used_calls, input_tokens=state.used_input_tokens),
        pending, now, input_tokens=input_tokens, manual=manual, confirmed_unknown=confirmed_unknown,
        coalesced=coalesced)
    if reason != "ready":
        return reason
    state.used_calls += 1
    state.used_input_tokens += input_tokens
    state.request_id, state.phase = str(request_id), "generating"
    db.flush()
    return "reserved"
