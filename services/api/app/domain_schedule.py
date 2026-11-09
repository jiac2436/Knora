"""Coalesce domain changes into explicitly authorized, bounded automatic plans."""
import json
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, select

from . import domain_builds as builds
from .ai import gateway_configured
from .domain_processing import (DomainBudgetUsage, DomainPendingWork, DomainProcessingPolicy,
                                dispatch_reason)
from .domain_runtime import _aware, locked_state
from .domain_summary_jobs import NodeEnqueue
from .errors import ApiError
from .models import (DomainBuild, DomainProcessingPreference, DomainProcessingState,
                     DomainSummaryArtifact, DomainSummaryJob, utcnow)


def is_automatic(build):
    # v12/manual plans did not have this optional JSON field.
    return bool(build and json.loads(build.plan_json).get('automatic', False))


def automatic_paused(db, build):
    if not is_automatic(build):
        return False
    preference = db.get(DomainProcessingPreference, (build.domain_id, build.scope))
    if preference is None:
        return False  # Execution will invalidate changed authorization normally.
    return DomainProcessingPolicy.model_validate_json(preference.policy_json).paused


def queued_job_build(db, job):
    identifier = json.loads(job.snapshot_json).get('build_id')
    return db.get(DomainBuild, identifier) if identifier else None


def update_decision(db, state, preference, now):
    """No writes or model calls; return reason and optionally a frozen local plan."""
    if state.scope == 'app':
        from .domain_full_analysis import active_full_job
        if active_full_job(db, state.owner_id, state.domain_id):
            return 'running', None
    if preference is None:
        return 'disabled', None
    policy = DomainProcessingPolicy.model_validate_json(preference.policy_json)
    if not policy.automatic_updates:
        return 'automatic_updates_disabled', None
    reason = dispatch_reason(policy,
        DomainBudgetUsage(calls=state.used_calls, input_tokens=state.used_input_tokens),
        DomainPendingWork(generation=state.generation, phase=state.phase,
            dirty_since=_aware(state.dirty_since), last_changed_at=_aware(state.last_changed_at)),
        now, input_tokens=1)
    if reason != 'ready':
        return reason, None
    active = db.scalar(select(DomainBuild.id).where(DomainBuild.domain_id == state.domain_id,
        DomainBuild.scope == state.scope, DomainBuild.status.in_(('pending', 'running'))))
    node = db.scalar(select(DomainSummaryJob.id).where(DomainSummaryJob.domain_id == state.domain_id,
        DomainSummaryJob.scope == state.scope, DomainSummaryJob.status.in_(('pending', 'running'))))
    if active or node:
        return 'running', None
    prior = db.scalar(select(DomainBuild.id).where(DomainBuild.domain_id == state.domain_id,
        DomainBuild.scope == state.scope, DomainBuild.generation == state.generation,
        DomainBuild.authorization_id == preference.authorization_id,
        func.json_extract(DomainBuild.plan_json, '$.automatic') == 1))
    if prior:
        return 'previous_plan_requires_manual_retry', None
    # Authorization alone, a topic build or a restored artifact never initiates
    # a first paid domain build. Require a successfully completed manual tree.
    baseline = db.scalar(select(DomainBuild).where(DomainBuild.owner_id == state.owner_id,
        DomainBuild.domain_id == state.domain_id, DomainBuild.scope == state.scope,
        DomainBuild.status == 'completed', func.json_extract(DomainBuild.plan_json, '$.topic_id').is_(None),
        func.coalesce(func.json_extract(DomainBuild.plan_json, '$.automatic'), 0) == 0)
        .order_by(DomainBuild.completed_at.desc(), DomainBuild.id.desc()).limit(1))
    if baseline is None:
        return 'first_build_requires_confirmation', None
    previous = json.loads(baseline.plan_json)
    from .model_configuration import model_binding_matches
    if (previous['rules'] != builds.RULES or baseline.gateway_url != builds.settings.ai_gateway_url.rstrip('/')
            or not model_binding_matches(previous, builds.settings)):
        return 'configuration_requires_confirmation', None
    root_id = str(uuid5(UUID(baseline.id), 'root'))
    if db.get(DomainSummaryArtifact, root_id) is None:
        return 'first_build_requires_confirmation', None
    if not gateway_configured(builds.settings):
        return 'gateway_unavailable', None
    try:
        plan = builds.prepare_plan(db, state.owner_id, state.domain_id, state.scope)
    except ApiError as error:
        return error.code, None
    if plan['manifest']['structure_sha256'] != previous['manifest']['structure_sha256']:
        return 'structure_requires_confirmation', None
    budget = builds.plan_budget(plan)
    if (plan['coverage']['source_count'] > policy.automatic_max_sources
            or budget['total_steps'] > policy.automatic_max_steps):
        return 'scope_requires_confirmation', None
    if (state.used_calls + budget['calls'] > policy.max_calls
            or state.used_input_tokens + budget['input_token_reservation'] > policy.max_input_tokens):
        return 'budget_exhausted', None
    return 'ready', plan


def schedule_domain_once(db, now=None):
    """Queue at most one due scope; no HTTP and no automatic budget renewal."""
    now = now or utcnow()
    candidates = db.execute(select(DomainProcessingState.owner_id, DomainProcessingState.domain_id,
        DomainProcessingState.scope).join(DomainProcessingPreference,
        (DomainProcessingPreference.domain_id == DomainProcessingState.domain_id)
        & (DomainProcessingPreference.scope == DomainProcessingState.scope))
        .where(DomainProcessingState.dirty_since.is_not(None))
        .where(func.json_extract(DomainProcessingPreference.policy_json, '$.automatic_updates') == 1,
               func.json_extract(DomainProcessingPreference.policy_json, '$.enabled') == 1,
               func.coalesce(func.json_extract(DomainProcessingPreference.policy_json, '$.paused'), 0) == 0)
        .order_by(DomainProcessingState.dirty_since, DomainProcessingState.domain_id)).all()
    for owner, domain, scope in candidates:
        state = locked_state(db, owner, domain, scope)
        preference = db.get(DomainProcessingPreference, (domain, scope))
        reason, plan = update_decision(db, state, preference, now)
        if reason != 'ready':
            db.rollback()
            continue
        # Same scope/generation/authorization has one stable automatic identity.
        identifier = str(uuid5(NAMESPACE_URL, f'domain-auto/{owner}/{domain}/{scope}/{state.generation}/{preference.authorization_id}'))
        body = NodeEnqueue(request_id=identifier, scope=scope, preview_hash=builds.plan_hash(plan))
        try:
            builds.enqueue_build(db, owner, domain, body, automatic=True)
        except ApiError as error:
            if error.code != 'model_configuration_pending':
                raise
            db.rollback()
            return False
        db.commit()
        return True
    return False


def schedule_view(db, owner, domain, scope, now=None):
    state = locked_state(db, owner, domain, scope)
    preference = db.get(DomainProcessingPreference, (domain, scope))
    reason, plan = update_decision(db, state, preference, now or utcnow())
    policy = DomainProcessingPolicy.model_validate_json(preference.policy_json) if preference else DomainProcessingPolicy()
    latest = db.scalar(select(DomainBuild).where(DomainBuild.owner_id == owner,
        DomainBuild.domain_id == domain, DomainBuild.scope == scope)
        .order_by(DomainBuild.created_at.desc(), DomainBuild.id.desc()).limit(1))
    latest_view = builds.build_view(db, latest) if latest else None
    if latest_view is not None:
        latest_view['topic_id'] = json.loads(latest.plan_json).get('topic_id')
    return dict(scope=scope, generation=state.generation, phase=state.phase,
        dirty_since=_aware(state.dirty_since), last_changed_at=_aware(state.last_changed_at),
        policy=policy.model_dump(), automatic_status=reason,
        remaining_calls=max(0, policy.max_calls - state.used_calls),
        remaining_input_tokens=max(0, policy.max_input_tokens - state.used_input_tokens),
        next_plan_budget=builds.plan_budget(plan) if plan else None,
        latest_build=latest_view)
