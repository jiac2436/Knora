"""Bounded processing trees, explicit consent and reusable original-backed steps."""
import hashlib
import json
from uuid import UUID, NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .ai import gateway_configured
from .auth import AppPrincipal, require_app_principal
from .config import settings
from .database import get_db
from .domain_contract import DomainContextSummary, DomainSummaryRequest, DomainSummaryResponse
from .domain_inputs import freeze_domain_inputs, manifest_fingerprint
from .domain_processing import DomainInputManifest, DomainProcessingPolicy
from .domain_runtime import locked_state
from .domain_summary_inputs import document_input
from .domain_node_inputs import partition_document, bound_parent_contexts
from .domain_summary_jobs import NodeSelection, NodeEnqueue, encode, input_reservation
from .domain_topics import require_owned_domain, require_topic, source_scope
from .errors import ApiError
from .models import (DomainBuild, DomainSummaryJob, DomainSummaryArtifact, DomainProcessingPreference,
                     DomainTopic, DomainTopicMember, Source, utcnow)

router = APIRouter(prefix='/v1/domains', tags=['domain-builds'])
RULES = 'bounded-tree-v2'
GENERATION_REVISION = 'bounded-attribution-v3-reasoning'
# Parent inputs depend on responses, so authorize a safe upper bound before send.
PARENT_INPUT_RESERVATION = 100000
MAX_STEPS = 256


def scope_manifest(db, owner, domain, scope, topic_id=None):
    require_owned_domain(db, owner, domain)
    if topic_id:
        topic = require_topic(db, owner, domain, topic_id)
        if topic.scope != scope:
            raise ApiError(404, 'not_found', '主题不存在')
        ids = frozenset(db.scalars(source_scope(owner, domain, scope).with_only_columns(Source.id).where(
            Source.id.in_(select(DomainTopicMember.source_id).where(
                DomainTopicMember.topic_id == topic_id, DomainTopicMember.included.is_(True))))))
    else:
        ids = None
    return freeze_domain_inputs(db, owner, domain, scope=scope, allowed_source_ids=ids)


def current_manifest(db, owner, domain, scope, frozen, topic_id=None, validation_context=None):
    manifest = (validation_context.manifest(owner, domain, scope, topic_id=topic_id) if validation_context
                else scope_manifest(db, owner, domain, scope, topic_id))
    if manifest_fingerprint(manifest) != manifest_fingerprint(DomainInputManifest.model_validate(frozen)):
        raise ApiError(409, 'stale_build', '知识域输入或授权范围已变化，请重新预览加工计划')


def step_key(frozen):
    request = dict(frozen['request'], request_id='content')
    summary_mode = request.get('contract_version') == 2
    if summary_mode:
        from .domain_overview_plan import RULES as summary_rules, GENERATION_REVISION as summary_revision
    return hashlib.sha256(encode(dict(request=request, manifest=frozen['manifest'], rules=summary_rules if summary_mode else RULES,
        generation_revision=summary_revision if summary_mode else GENERATION_REVISION,
        published_topic_id=frozen.get('published_topic_id'), topic_manifest=frozen.get('topic_manifest'))).encode()).hexdigest()


def reusable_artifact(db, owner, domain, scope, frozen):
    key = step_key(frozen)
    return db.scalar(select(DomainSummaryArtifact.id).where(DomainSummaryArtifact.owner_id == owner,
        DomainSummaryArtifact.domain_id == domain, DomainSummaryArtifact.scope == scope,
        func.json_extract(DomainSummaryArtifact.snapshot_json, '$.step_key') == key)
        .order_by(DomainSummaryArtifact.created_at.desc(), DomainSummaryArtifact.id.desc()).limit(1))


def prepare_plan(db, owner, domain, scope, topic_id=None, *, excluded_source_ids=frozenset()):
    if db.get_bind().dialect.name != 'sqlite':
        raise ApiError(409, 'local_sqlite_required', '概览加工目前只支持本地SQLite')
    from .domain_overview_plan import prepare_summary_plan
    from .model_configuration import frozen_model_binding
    return {**prepare_summary_plan(db, owner, domain, scope, topic_id, excluded_source_ids=excluded_source_ids), **frozen_model_binding(settings)}


def prepare_legacy_plan(db, owner, domain, scope, topic_id=None):
    if db.get_bind().dialect.name != 'sqlite':
        raise ApiError(409, 'local_sqlite_required', '分批加工目前只支持本地SQLite')
    manifest = scope_manifest(db, owner, domain, scope, topic_id)
    if len(manifest.sources) > 5000:
        raise ApiError(409, 'plan_too_large', '当前加工计划最多纳入5000篇资料，请缩小主题范围')
    node = require_topic(db, owner, domain, topic_id) if topic_id else require_owned_domain(db, owner, domain)
    namespace = uuid5(NAMESPACE_URL, owner + '/' + domain + '/' + scope)
    source_ids = {s.source_id for s in manifest.sources}
    groups = []
    if topic_id:
        groups.append((topic_id, sorted(source_ids)))
    else:
        covered = set()
        topics = db.scalars(select(DomainTopic).where(DomainTopic.owner_id == owner,
            DomainTopic.domain_id == domain, DomainTopic.scope == scope).order_by(DomainTopic.id)).all()
        memberships = {}
        for topic in topics:
            ids = set(db.scalars(select(DomainTopicMember.source_id).where(
                DomainTopicMember.topic_id == topic.id, DomainTopicMember.included.is_(True)))) & source_ids
            memberships[topic.id] = ids
        # Respect human topic choices. Only an untouched automatic candidate
        # with exactly the same full membership can be omitted from a domain
        # build. Partial overlaps and human-fixed topics retain their branches.
        manual_sets = {frozenset(memberships[t.id]) for t in topics
            if (not t.automatic_key or t.name_fixed or t.parent_id) and memberships[t.id]}
        edited_topics = set(db.scalars(select(DomainTopicMember.topic_id).where(
            DomainTopicMember.topic_id.in_([t.id for t in topics]), DomainTopicMember.manual.is_(True))))
        for topic in topics:
            ids = memberships[topic.id]
            duplicate_candidate = (topic.automatic_key and not topic.name_fixed and not topic.parent_id
                and topic.id not in edited_topics and frozenset(ids) in manual_sets)
            if duplicate_candidate:
                continue
            if ids:
                groups.append((topic.id, sorted(ids))); covered.update(ids)
        if source_ids - covered:
            groups.append((None, sorted(source_ids - covered)))
    steps, excluded, included, topic_roots = [], {}, set(), []

    def combine(children, title):
        while len(children) > 1:
            parents = []
            for start in range(0, len(children), 2):
                pair = children[start:start + 2]
                if len(pair) == 1:
                    parents.append(pair[0]); continue
                key = 'merge:' + hashlib.sha256(':'.join(pair).encode()).hexdigest()[:32]
                steps.append(dict(key=key, children=pair, title=title, node_id=str(uuid5(namespace, key)),
                    input_tokens=PARENT_INPUT_RESERVATION, reuse_id=None))
                parents.append(key)
            children = parents
        return children[0] if children else None

    documents = {}
    for dependency in manifest.sources:
        context, evidence, reason = document_input(db, dependency)
        if reason:
            excluded[dependency.source_id] = reason
        elif len(evidence) > 80 or sum(len(e.text) for e in evidence.values()) > 18000:
            excluded[dependency.source_id] = 'single_document_exceeds_batch'
        else:
            documents[dependency.source_id] = (context, evidence)
    for selected_topic, ids in groups:
        batches, accepted, evidence, contexts = [], set(), {}, []
        title = require_topic(db, owner, domain, selected_topic).name if selected_topic else node.name
        for identifier in ids:
            if identifier not in documents: continue
            for context, originals in partition_document(*documents[identifier]):
                combined = {**evidence, **originals}
                if contexts and (len(contexts) == 10 or len(combined) > 40
                        or sum(len(e.text) for e in combined.values()) > 9000
                        or sum(len(c.text) for c in contexts) + len(context.text) > 6000):
                    batches.append((accepted, contexts, evidence))
                    accepted, contexts, evidence = set(), [], {}
                accepted.add(identifier); contexts.append(context); evidence.update(originals)
        if contexts: batches.append((accepted, contexts, evidence))
        leaves = []
        for accepted, contexts, evidence in batches:
            included.update(accepted)
            evidence_key = hashlib.sha256(':'.join(sorted(evidence)).encode()).hexdigest()[:16]
            key = 'leaf:' + (selected_topic or 'unclassified') + ':' + ','.join(sorted(accepted)) + ':' + evidence_key
            request = DomainSummaryRequest(request_id=str(uuid5(namespace, 'preview')), node_id=str(uuid5(namespace, key)),
                kind='topic', title=title, summaries=contexts, evidence=list(evidence.values()),
                max_cited_sources=10, max_cited_evidence=40, max_cited_characters=9000)
            subset = manifest.model_copy(update=dict(sources=[d for d in manifest.sources if d.source_id in accepted]))
            frozen = dict(request=request.model_dump(), manifest=subset.model_dump(),
                coverage=dict(source_count=len(accepted), included_source_count=len(accepted), excluded={},
                    all_sources_selected=True, original_text_complete=False,
                    basis='valid_document_summaries_and_cited_original_excerpts'))
            steps.append(dict(key=key, children=[], frozen=frozen, selector=selected_topic,
                input_tokens=input_reservation(frozen), reuse_id=reusable_artifact(db, owner, domain, scope, frozen)))
            leaves.append(key)
            if len(steps) > MAX_STEPS:
                raise ApiError(409, 'plan_too_large', '加工批次过多，请缩小主题范围并重新确认')
        root = combine(leaves, title)
        if root:
            topic_roots.append(root)
            if selected_topic:
                branch = next(s for s in steps if s['key'] == root)
                topic_manifest = scope_manifest(db, owner, domain, scope, selected_topic).model_dump()
                processed = {identifier for leaf in steps if leaf.get('selector') == selected_topic
                    for identifier in [d['source_id'] for d in leaf['frozen']['manifest']['sources']]}
                branch.update(published_topic_id=selected_topic, topic_manifest=topic_manifest,
                    topic_coverage=dict(source_count=len(ids), processed_source_count=len(processed),
                        excluded={identifier: excluded[identifier] for identifier in ids if identifier in excluded},
                        original_text_complete=False, basis='topic_branch_summaries_and_original_citations'))
                if not branch['children']:
                    branch['frozen'].update(published_topic_id=selected_topic, topic_manifest=topic_manifest)
                    branch['reuse_id'] = reusable_artifact(db, owner, domain, scope, branch['frozen'])
    if not steps:
        raise ApiError(409, 'no_eligible_summary', '没有有效单篇摘要；不会自动补做收费分析')
    root_key = combine(topic_roots, node.name)
    final = next(s for s in steps if s['key'] == root_key)
    if final['children'] and not final.get('published_topic_id'):
        final.update(key='root', title=node.name, node_id=topic_id or domain)
    else:
        steps.append(dict(key='root', children=[root_key], title=node.name, node_id=topic_id or domain,
            input_tokens=PARENT_INPUT_RESERVATION, reuse_id=None))
    if len(steps) > MAX_STEPS:
        raise ApiError(409, 'plan_too_large', '加工批次过多，请缩小主题范围并重新确认')
    return dict(rules=RULES, generation_revision=GENERATION_REVISION,
        manifest=manifest.model_dump(), topic_id=topic_id, steps=steps,
        coverage=dict(source_count=len(source_ids), processed_source_count=len(included),
            excluded={key: value for key, value in excluded.items() if key not in included},
            all_valid_summaries_processed=len(included) == len(source_ids), original_text_complete=False,
            basis='hierarchical_valid_summaries_and_selected_original_citations'))


def plan_hash(plan):
    return hashlib.sha256(encode(plan).encode()).hexdigest()


def plan_budget(plan):
    fresh = [s for s in plan['steps'] if not s['reuse_id']]
    return dict(calls=len(fresh), input_token_reservation=sum(s['input_tokens'] for s in fresh),
                reused_batches=len(plan['steps']) - len(fresh), total_steps=len(plan['steps']))


def enqueue_build(db, owner, domain, body, *, automatic=False):
    from .model_switch import require_accepting
    if db.get_bind().dialect.name != 'sqlite':
        raise ApiError(409, 'local_sqlite_required', '加工任务仅支持本地SQLite')
    state = locked_state(db, owner, domain, body.scope)
    prior = db.get(DomainBuild, body.request_id)
    if prior:
        old = json.loads(prior.plan_json)
        if (prior.owner_id != owner or prior.domain_id != domain or prior.scope != body.scope
                or old['topic_id'] != body.topic_id or plan_hash(old) != body.preview_hash
                or prior.confirmed_unknown != body.confirmed_unknown):
            raise ApiError(409, 'request_conflict', '相同计划编号不能改变输入或范围')
        return prior
    require_accepting(db)
    if body.scope == 'app':
        from .domain_full_analysis import active_full_job
        if active_full_job(db, owner, domain):
            raise ApiError(409, 'full_running', '知识域全量分析正在执行，请先查看或停止该任务')
    preference = db.get(DomainProcessingPreference, (domain, body.scope))
    if preference is None or not gateway_configured(settings):
        raise ApiError(409, 'disabled', '请先配置网关并明确授权预算')
    policy = DomainProcessingPolicy.model_validate_json(preference.policy_json)
    if not policy.enabled:
        raise ApiError(409, 'disabled', '请先明确授权预算')
    if automatic and (not policy.automatic_updates or policy.paused or state.phase in ('failed', 'unknown')):
        raise ApiError(409, 'automatic_update_blocked', '自动加工已暂停或需要人工处理')
    active = db.scalar(select(DomainBuild).where(DomainBuild.domain_id == domain, DomainBuild.scope == body.scope,
        DomainBuild.status.in_(('pending', 'running'))))
    active_nodes = db.scalars(select(DomainSummaryJob).where(DomainSummaryJob.domain_id == domain,
        DomainSummaryJob.scope == body.scope, DomainSummaryJob.status.in_(('pending', 'running')))).all()
    # An explicit update may replace a paused automatic plan, but cannot cancel
    # an in-flight request or reclaim its already reserved budget.
    replace_paused = bool(active and not automatic and policy.paused
        and json.loads(active.plan_json).get('automatic', False)
        and all(job.status == 'pending' and json.loads(job.snapshot_json).get('build_id') == active.id
                for job in active_nodes))
    if (active or active_nodes) and not replace_paused:
        raise ApiError(409, 'running', '当前范围已有加工任务')
    if state.phase == 'unknown' and not body.confirmed_unknown:
        raise ApiError(409, 'unknown_requires_confirmation', '上次结果未知，需确认可能重复用量')
    plan = prepare_plan(db, owner, domain, body.scope, body.topic_id)
    if plan_hash(plan) != body.preview_hash:
        raise ApiError(409, 'stale_preview', '输入或可复用批次已变化，请重新预览')
    budget = plan_budget(plan)
    if state.used_calls + budget['calls'] > policy.max_calls or state.used_input_tokens + budget['input_token_reservation'] > policy.max_input_tokens:
        raise ApiError(409, 'budget_exhausted', '剩余预算不足以完成整个计划')
    if automatic and (plan['coverage']['source_count'] > policy.automatic_max_sources
            or budget['total_steps'] > policy.automatic_max_steps):
        raise ApiError(409, 'scope_requires_confirmation', '自动加工范围超出授权上限')
    if replace_paused:
        active.status, active.last_error, active.completed_at = 'cancelled', '被用户明确更新替换，保留历史用量', utcnow()
        for job in active_nodes:
            job.status, job.last_error, job.completed_at = 'cancelled', '用户明确更新替换，未发送', utcnow()
    if automatic:
        plan['automatic'] = True
    build = DomainBuild(id=body.request_id, owner_id=owner, domain_id=domain, scope=body.scope,
        generation=state.generation, authorization_id=preference.authorization_id,
        gateway_url=settings.ai_gateway_url.rstrip('/'), plan_json=encode(plan), confirmed_unknown=body.confirmed_unknown)
    if state.phase != 'unknown':
        state.phase = 'dirty'
    if state.dirty_since is None:
        state.dirty_since = state.last_changed_at = utcnow()
    db.add(build); db.flush()
    return build


def build_view(db, build):
    plan = json.loads(build.plan_json)
    progress = []
    for step in plan['steps']:
        identifier = str(uuid5(UUID(build.id), step['key']))
        job = db.get(DomainSummaryJob, identifier)
        artifact_id = step['reuse_id'] or (identifier if job and job.status == 'completed' else None)
        progress.append(dict(key=step['key'], status='reused' if step['reuse_id'] else job.status if job else 'waiting',
                             artifact_id=artifact_id))
    completed = sum(s['status'] in ('completed', 'reused') for s in progress)
    current = next((step for step, state in zip(plan['steps'], progress)
                    if state['status'] in ('running', 'pending')), None)
    return dict(id=build.id, status=build.status, automatic=plan.get('automatic', False),
                coverage=plan['coverage'], budget=plan_budget(plan),
                steps=progress, completed_steps=completed, total_steps=len(progress),
                current_step_title=(current.get('title') or current.get('frozen', {}).get('request', {}).get('title')) if current else None,
                last_error=build.last_error)


@router.post('/{domain_id}/analysis/build/preview')
def preview_build(domain_id: str, body: NodeSelection, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    plan = prepare_plan(db, principal.owner_id, domain_id, body.scope, body.topic_id)
    return dict(preview_hash=plan_hash(plan), coverage=plan['coverage'], **plan_budget(plan))


@router.post('/{domain_id}/analysis/build/jobs')
def create_build(domain_id: str, body: NodeEnqueue, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    build = enqueue_build(db, principal.owner_id, domain_id, body)
    db.commit()
    return build_view(db, build)


@router.get('/{domain_id}/analysis/build/jobs/{build_id}')
def get_build(domain_id: str, build_id: str, principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    require_owned_domain(db, principal.owner_id, domain_id)
    build = db.get(DomainBuild, build_id)
    if build is None or build.owner_id != principal.owner_id or build.domain_id != domain_id:
        raise ApiError(404, 'not_found', '加工任务不存在')
    return build_view(db, build)


@router.post('/{domain_id}/analysis/schedule-state')
def get_schedule_state(domain_id: str, body: NodeSelection,
                       principal: AppPrincipal = Depends(require_app_principal), db: Session = Depends(get_db)):
    from .domain_schedule import schedule_view
    if body.topic_id is not None:
        raise ApiError(422, 'domain_scope_required', '自动更新状态属于整个知识域')
    view = schedule_view(db, principal.owner_id, domain_id, body.scope)
    db.commit()
    return view


def materialize_step(db, build, plan, step):
    identifier = str(uuid5(UUID(build.id), step['key']))
    if not step['children']:
        frozen = json.loads(encode(step['frozen']))
        frozen['request']['request_id'] = identifier
    elif plan.get('rules', RULES) == 'summary-budget-v3':
        from .domain_overview_plan import materialize_summary_parent
        frozen = materialize_summary_parent(db, build, plan, step, identifier)
    else:
        by_key = {s['key']: s for s in plan['steps']}
        contexts, evidence = [], {}
        child_ids, dependencies = [], set()
        for key in step['children']:
            child = by_key[key]
            artifact_id = child['reuse_id'] or str(uuid5(UUID(build.id), key))
            artifact = db.get(DomainSummaryArtifact, artifact_id)
            if artifact is None or (artifact.owner_id, artifact.domain_id, artifact.scope) != (build.owner_id, build.domain_id, build.scope):
                raise ApiError(409, 'missing_child', '已完成批次产物不存在或范围不符')
            child_frozen = json.loads(artifact.snapshot_json)
            response = DomainSummaryResponse.model_validate_json(artifact.response_json)
            from .domain_contract import validate_domain_summary
            child_request = DomainSummaryRequest.model_validate(child_frozen['request'])
            if response.request_id != child_request.request_id:
                raise ApiError(409, 'invalid_child', '批次响应与请求不符')
            validate_domain_summary(child_request, response.result)
            points = [response.result.overview, *response.result.insights, *response.result.limitations]
            citations = sorted({c for p in points for c in p.citations})
            originals = {e['id']: e for e in child_frozen['request']['evidence']}
            evidence.update({c: originals[c] for c in citations})
            contexts.append(DomainContextSummary(node_id=artifact_id, kind='topic',
                text='\n'.join(p.text for p in points), citations=citations,
                source_ids=sorted({originals[c]['source_id'] for c in citations})))
            child_ids.append(artifact_id)
            dependencies.update(s['source_id'] for s in child_frozen['manifest']['sources'])
        # The contract accepts 18,000 original characters, 20 sources and 80
        # evidence items. Each intermediate retains at most half, so two children
        # fit exactly. The terminal root has no parent and may retain the union.
        terminal = step['key'] == 'root'
        contexts, evidence, selection = bound_parent_contexts(contexts, evidence,
            characters=18000 if terminal else 9000,
            sources=20 if terminal else 10, items=80 if terminal else 40)
        request = DomainSummaryRequest(request_id=identifier, node_id=step['node_id'], kind='domain',
            title=step['title'], summaries=contexts, evidence=list(evidence.values()),
            max_cited_sources=20 if terminal else 10,
            max_cited_evidence=80 if terminal else 40,
            max_cited_characters=18000 if terminal else 9000)
        frozen = dict(request=request.model_dump(), manifest=freeze_domain_inputs(db, build.owner_id, build.domain_id,
            scope=build.scope, allowed_source_ids=frozenset(dependencies)).model_dump(), coverage=plan['coverage'],
                      child_artifact_ids=child_ids, evidence_selection=selection)
    frozen['processing_manifest'] = freeze_domain_inputs(db, build.owner_id, build.domain_id,
        scope=build.scope, allowed_source_ids=frozenset(d['source_id'] for d in frozen['manifest']['sources'])).model_dump()
    if step.get('published_topic_id'):
        frozen.update(published_topic_id=step['published_topic_id'], topic_manifest=step['topic_manifest'])
        frozen['coverage'] = frozen['topic_coverage'] = step['topic_coverage']
    elif step['key'] != 'root':
        count = len(frozen['processing_manifest']['sources'])
        frozen['coverage'] = dict(source_count=count, processed_source_count=count,
            original_text_complete=False, basis='bounded_processing_batch')
    frozen.update(build_id=build.id, build_mode=plan.get('rules', RULES), generation_revision=plan.get('generation_revision', GENERATION_REVISION),
        step=step['key'], target_topic_id=plan['topic_id'],
        step_key=step_key(frozen))
    if 'model_binding' in plan:
        frozen['model_binding'] = plan['model_binding']
    if step['key'] == 'root':
        frozen['global_manifest'] = plan['manifest']
    if input_reservation(frozen) > step['input_tokens']:
        raise ApiError(409, 'parent_budget_exceeded', '实际合成输入超过已确认上限，未发送')
    return frozen


def validate_build_job(db, job, frozen):
    build = db.get(DomainBuild, frozen['build_id'])
    if build is None or (build.owner_id, build.domain_id, build.scope) != (job.owner_id, job.domain_id, job.scope):
        raise ApiError(409, 'stale_build', '加工计划不存在或范围不符')
    if build.status not in ('pending', 'running'):
        raise ApiError(409, 'stale_build', '加工计划已停止')
    plan = json.loads(build.plan_json)
    state = locked_state(db, build.owner_id, build.domain_id, build.scope)
    if plan.get('full_job_id'):
        from .domain_full_runtime import validate_full_build_authorization
        validate_full_build_authorization(db, build)
    if state.generation != build.generation:
        raise ApiError(409, 'stale_build', '知识域输入代次已变化')
    step = next((s for s in plan['steps'] if s['key'] == frozen['step']), None)
    if step is None or materialize_step(db, build, plan, step) != frozen:
        raise ApiError(409, 'stale_build', '加工批次输入已变化')


def validate_build_artifact(db, owner, domain, scope, frozen, response, validation_context=None):
    """No dependency on transient build jobs; survives backup and restoration."""
    from .domain_contract import validate_domain_summary
    request = DomainSummaryRequest.model_validate(frozen['request'])
    if frozen.get('published_topic_id'):
        topic_id = frozen['published_topic_id']
        topic = validation_context.topic(owner, domain, scope, topic_id) if validation_context else require_topic(db, owner, domain, topic_id)
        if topic.scope != scope or request.title != topic.name or response.request_id != request.request_id:
            raise ApiError(409, 'snapshot_binding_mismatch', '主题映射范围不符')
        validate_domain_summary(request, response.result)
        if validation_context:
            validation_context.check()
        else:
            locked_state(db, owner, domain, scope)
        current_manifest(db, owner, domain, scope, frozen['topic_manifest'], topic_id, validation_context=validation_context)
        from .domain_processing_nodes import validate_processing
        validate_processing(db, owner, domain, scope, frozen, response, validation_context=validation_context)
        return
    manifest = DomainInputManifest.model_validate(frozen['global_manifest'])
    if (manifest.owner_id, manifest.domain_id, manifest.scope) != (owner, domain, scope) or response.request_id != request.request_id:
        raise ApiError(409, 'snapshot_binding_mismatch', '概览范围不符')
    if frozen['step'] != 'root' or request.node_id != (frozen['target_topic_id'] or domain):
        raise ApiError(409, 'not_root', '中间批次不能作为最终概览')
    validate_domain_summary(request, response.result)
    if validation_context:
        validation_context.check()
    else:
        locked_state(db, owner, domain, scope)
    current_manifest(db, owner, domain, scope, frozen['global_manifest'], frozen['target_topic_id'], validation_context=validation_context)
