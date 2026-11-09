"""Advance one frozen processing tree checkpoint, without sending HTTP."""
import json
from uuid import UUID, uuid5

from sqlalchemy import select, update

from app.domain_builds import current_manifest, materialize_step
from app.domain_processing import DomainProcessingPolicy
from app.domain_runtime import locked_state
from app.domain_summary_jobs import encode
from app.domain_schedule import automatic_paused
from app.errors import ApiError
from app.config import settings
from app.model_configuration import model_binding_matches
from app.models import DomainBuild, DomainProcessingPreference, DomainProcessingState, DomainSummaryJob, DomainSummaryArtifact, utcnow


def paused_checkpoint(db, build):
    state = db.get(DomainProcessingState, (build.domain_id, build.scope))
    # Closing a failed/unknown checkpoint sends no request, even while paused.
    return automatic_paused(db, build) and (state is None or state.phase not in ('failed', 'unknown'))


def interrupt_builds(db):
    # Completed child artifacts remain reusable. No plan with an unknown paid
    # child continues automatically after a crash.
    builds = db.scalars(select(DomainBuild).where(DomainBuild.status.in_(('pending', 'running')))).all()
    for build in builds:
        plan = json.loads(build.plan_json)
        ids = [str(uuid5(UUID(build.id), step['key'])) for step in plan['steps']]
        if db.scalar(select(DomainSummaryJob.id).where(DomainSummaryJob.id.in_(ids), DomainSummaryJob.status == 'unknown')):
            build.status, build.last_error, build.completed_at = 'unknown', '批次结果未知，未自动继续', utcnow()


def run_build_once(db):
    candidates = db.scalars(select(DomainBuild).where(DomainBuild.status.in_(('pending', 'running')))
                           .order_by(DomainBuild.created_at, DomainBuild.id)).all()
    build = next((candidate for candidate in candidates if not paused_checkpoint(db, candidate)), None)
    if build is None:
        return False
    state = locked_state(db, build.owner_id, build.domain_id, build.scope)
    db.refresh(build)
    if build.status not in ('pending', 'running'):
        db.rollback(); return False
    if paused_checkpoint(db, build):
        db.rollback(); return False
    plan = json.loads(build.plan_json)
    try:
        if not model_binding_matches(plan, settings):
            raise ApiError(409, 'configuration_changed', '模型配置代次已变化或旧任务缺少绑定，未继续调用')
        if state.generation != build.generation:
            raise ApiError(409, 'stale_build', '知识域输入代次已变化')
        if build.status == 'pending':
            current_manifest(db, build.owner_id, build.domain_id, build.scope, plan['manifest'], plan['topic_id'])
        if plan.get('full_job_id'):
            from app.domain_full_runtime import validate_full_build_authorization
            validate_full_build_authorization(db, build)
        else:
            preference = db.get(DomainProcessingPreference, (build.domain_id, build.scope))
            if (preference is None or preference.authorization_id != build.authorization_id
                    or not DomainProcessingPolicy.model_validate_json(preference.policy_json).enabled):
                raise ApiError(409, 'authorization_changed', '加工授权已变化，未继续')
        for step in plan['steps']:
            if step['reuse_id']:
                artifact = db.get(DomainSummaryArtifact, step['reuse_id'])
                if artifact is None:
                    raise ApiError(409, 'missing_reuse', '可复用批次已清理，请重新预览')
                continue
            identifier = str(uuid5(UUID(build.id), step['key']))
            job = db.get(DomainSummaryJob, identifier)
            if job is not None:
                if job.status == 'completed':
                    continue
                if job.status in ('pending', 'running'):
                    db.rollback(); return False
                build.status, build.last_error, build.completed_at = job.status, job.last_error, utcnow()
                db.commit(); return True
            frozen = materialize_step(db, build, plan, step)
            db.add(DomainSummaryJob(id=identifier, owner_id=build.owner_id, domain_id=build.domain_id, scope=build.scope,
                generation=build.generation, authorization_id=build.authorization_id, gateway_url=build.gateway_url,
                input_tokens=step['input_tokens'], confirmed_unknown=build.confirmed_unknown,
                snapshot_json=encode(frozen)))
            build.status = 'running'
            db.commit(); return True
        current_manifest(db, build.owner_id, build.domain_id, build.scope, plan['manifest'], plan['topic_id'])
        build.status, build.completed_at = 'completed', utcnow()
        state.phase, state.dirty_since, state.last_changed_at = 'partial', None, None
        db.commit(); return True
    except ApiError as error:
        build.status, build.last_error, build.completed_at = 'stale', error.message, utcnow()
        if state.phase != 'unknown': state.phase = 'dirty'
        # Unsent pending children can no longer reach the provider.
        ids = [str(uuid5(UUID(build.id), step['key'])) for step in plan['steps']]
        db.execute(update(DomainSummaryJob).where(DomainSummaryJob.id.in_(ids), DomainSummaryJob.status == 'pending')
                   .values(status='stale', last_error='加工计划已失效，未发送', completed_at=utcnow()))
        db.commit(); return True
