"""Transactional dispatch of a one-time full-domain processing grant."""
import json

from .ai import gateway_configured
from .config import settings
from .domain_summary_jobs import encode
from .errors import ApiError
from .models import DomainFullAnalysisJob

ACTIVE = ('pending', 'running')


def validate_full_build_authorization(db, build):
    """One explicit workflow grant, independent of automatic-update preferences."""
    from app.domain_full_analysis import ensure_inputs_current
    plan = json.loads(build.plan_json)
    full_id = plan.get('full_job_id')
    full = db.get(DomainFullAnalysisJob, full_id) if full_id else None
    if (full is None or full.status not in ACTIVE or full.phase != 'overview'
            or (full.owner_id, full.domain_id, full.build_id) != (build.owner_id, build.domain_id, build.id)
            or build.scope != 'app' or build.authorization_id != full.id):
        raise ApiError(409, 'full_authorization_stopped', '全量分析授权已停止，未继续调用')
    ensure_inputs_current(db, full)
    if build.gateway_url != settings.ai_gateway_url.rstrip('/') or not gateway_configured(settings):
        raise ApiError(409, 'gateway_changed', 'AI 网关已变化或不可用，未继续调用')
    return full


def reserve_full_node_call(db, build, node_job, state):
    """Persist conservative usage before HTTP without expanding prior policy."""
    full = validate_full_build_authorization(db, build)
    budget = json.loads(full.budget_json)
    if state.request_id == node_job.id:
        return 'already_reserved'
    if (budget.get('used_calls', 0) + 1 > budget['calls']
            or budget.get('used_input_tokens', 0) + node_job.input_tokens > budget['input_token_reservation']):
        return 'budget_exhausted'
    budget['used_calls'] = budget.get('used_calls', 0) + 1
    budget['used_input_tokens'] = budget.get('used_input_tokens', 0) + node_job.input_tokens
    full.budget_json = encode(budget)
    state.request_id, state.phase = node_job.id, 'generating'
    db.flush()
    return 'reserved'


