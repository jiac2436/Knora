"""Summary-first navigation: one call when bounded, stable batches only when needed."""
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, NAMESPACE_URL, uuid5

from pydantic import ValidationError
from sqlalchemy import select

from .domain_contract import (DomainContextSummary, DomainSummaryRequest, DomainSummaryResponse,
    SUMMARY_INPUT_BUDGET, SUMMARY_WIRE_BUDGET, summary_input_reservation)
from .domain_processing import DomainInputManifest
from .domain_summary_inputs import document_input
from .errors import ApiError
from .models import AIArtifact, DomainTopic, DomainTopicMember, Source

RULES = 'summary-budget-v3'
GENERATION_REVISION = 'summary-navigation-v2'
# Three full intermediate outputs (2,200 chars each), references and envelope
# fit the conservative input budget, including four-byte Unicode text.
FAN_IN = 3


def summary_document(db, dependency):
    context, evidence, reason = document_input(db, dependency)
    if reason:
        return context, evidence, reason
    artifact = db.get(AIArtifact, dependency.summary_artifact_id)
    result = json.loads(artifact.result_json)
    source = db.get(Source, dependency.source_id)
    # Preserve all supplied text; a long summary is split by planning, never sliced.
    overview = result.get('overview') or {}
    # Source anchors travel separately. Repeating their UUIDs inside prose
    # wastes context and encourages the provider to echo the wrong metadata.
    text = json.dumps(dict(title=source.title, overview=overview.get('text', ''),
        insights=[p['text'] for p in result.get('insights', [])],
        limitations=[p['text'] for p in result.get('limitations', [])]),
        ensure_ascii=False, separators=(',', ':'))
    # Older analyses may not have compact fields; their valid summary is still useful.
    if not result.get('overview'):
        text = source.title + '\n' + context.text
    if len(text) > 4000:
        return None, {}, 'summary_too_long'
    return context.model_copy(update=dict(text=text)), evidence, None


def request_for(identifier, node, title, documents, *, kind='domain', purpose='overview'):
    evidence = {key: value for _, originals in documents for key, value in originals.items()}
    request = DomainSummaryRequest(contract_version=2, request_id=identifier,
        node_id=node, kind=kind, purpose=purpose, title=title,
        summaries=[context for context, _ in documents], evidence=list(evidence.values()))
    if len(request.model_dump_json().encode()) > SUMMARY_WIRE_BUDGET:
        raise ValueError('Local summary envelope exceeds wire budget')
    return request


def prepare_summary_plan(db, owner, domain, scope, topic_id=None, *, excluded_source_ids=frozenset()):
    from .domain_builds import scope_manifest, reusable_artifact, materialize_step, MAX_STEPS
    from .domain_topics import require_topic, require_owned_domain
    manifest = scope_manifest(db, owner, domain, scope, topic_id)
    if len(manifest.sources) > 5000:
        raise ApiError(409, 'plan_too_large', '当前加工计划最多纳入5000篇资料')
    node = require_topic(db, owner, domain, topic_id) if topic_id else require_owned_domain(db, owner, domain)
    namespace = uuid5(NAMESPACE_URL, owner + '/' + domain + '/' + scope + '/' + RULES)
    documents, excluded = {}, {}
    for dep in manifest.sources:
        if dep.source_id in excluded_source_ids:
            excluded[dep.source_id] = 'skipped_stale_analysis'
            continue
        context, evidence, reason = summary_document(db, dep)
        if reason:
            excluded[dep.source_id] = reason
        else:
            documents[dep.source_id] = (context, evidence)
    if not documents:
        raise ApiError(409, 'no_eligible_summary', '没有有效单篇概览；不会自动补做收费分析')
    def coverage(ids):
        return dict(source_count=len(ids), processed_source_count=len(ids),
            all_valid_summaries_processed=True, original_text_complete=False,
            basis='document_overviews_navigation', excluded={})
    def fits(ids, purpose='batch'):
        try:
            request_for(str(uuid5(namespace, 'preview')), domain, node.name,
                [documents[i] for i in ids], kind='topic' if topic_id else 'domain', purpose=purpose)
            return True
        except (ValueError, ValidationError):
            return False
    ids = sorted(documents)
    all_coverage = dict(coverage(ids), source_count=len(manifest.sources), excluded=excluded,
        all_valid_summaries_processed=not excluded)
    steps = []
    def leaf(ids, key, title, final=False):
        request = request_for(str(uuid5(namespace, 'preview')), (topic_id or domain) if final else str(uuid5(namespace, key)),
            title, [documents[i] for i in ids], kind='topic' if topic_id else 'domain',
            purpose='overview' if final else 'batch')
        subset = manifest.model_copy(update=dict(sources=[d for d in manifest.sources if d.source_id in set(ids)]))
        frozen = dict(request=request.model_dump(), manifest=subset.model_dump(), coverage=coverage(ids))
        if final:
            frozen['coverage'] = all_coverage
        steps.append(dict(key=key, children=[], frozen=frozen, input_tokens=summary_input_reservation(request),
            reuse_id=reusable_artifact(db, owner, domain, scope, frozen), title=title,
            node_id=request.node_id, source_ids=ids))
    if fits(ids, 'overview'):
        leaf(ids, 'root', node.name, True)
    else:
        # Assign each document once. Existing themes organize input but don't
        # force paid theme generation or duplicate overlapping memberships.
        primary = {}
        if not topic_id:
            rows = db.execute(select(DomainTopicMember.source_id, DomainTopic.id, DomainTopic.name)
                .join(DomainTopic, DomainTopic.id == DomainTopicMember.topic_id).where(
                    DomainTopic.owner_id == owner, DomainTopic.domain_id == domain,
                    DomainTopic.scope == scope, DomainTopicMember.included.is_(True))
                .order_by(DomainTopic.id))
            for source, tid, title in rows:
                primary.setdefault(source, (tid, title))
        def split(members, depth=0):
            if fits(members):
                return [members]
            if len(members) == 1:
                raise ApiError(409, 'summary_input_too_large', '单篇概览或本地引用数据超出加工上限，未遗漏该资料')
            if depth >= 256:
                raise ApiError(409, 'plan_too_large', '无法在输入预算内拆分资料')
            # Keep themes together where possible. Once a branch contains one
            # large theme, split it by stable document hash bits. Binary splits
            # here size *input batches*, not a two-at-a-time merge pipeline.
            themes = {primary.get(source, ('unclassified', ''))[0] for source in members}
            buckets = {}
            for source in members:
                atom = primary.get(source, ('unclassified', ''))[0] if len(themes) > 1 else source
                digest = int(hashlib.sha256(atom.encode()).hexdigest(), 16)
                buckets.setdefault((digest >> (255-depth)) & 1, []).append(source)
            return [part for key in sorted(buckets) for part in split(buckets[key], depth+1)]
        for batch in split(ids):
            batch = sorted(batch, key=lambda source: (primary.get(source, ('unclassified', ''))[0], source))
            themes = {primary.get(source, (topic_id or 'unclassified', node.name)) for source in batch}
            title = next(iter(themes))[1] if len(themes) == 1 else node.name
            key = 'batch:' + hashlib.sha256(','.join(sorted(batch)).encode()).hexdigest()[:32]
            leaf(batch, key, title)
        children = [s['key'] for s in steps]
        while len(children) > FAN_IN:
            parents = []
            for start in range(0, len(children), FAN_IN):
                chunk = children[start:start + FAN_IN]
                if len(chunk) == 1:
                    parents += chunk
                    continue
                key = 'merge:' + hashlib.sha256(':'.join(chunk).encode()).hexdigest()[:32]
                steps.append(dict(key=key, children=chunk, title=node.name, node_id=str(uuid5(namespace, key)),
                    input_tokens=SUMMARY_INPUT_BUDGET, reuse_id=None))
                parents.append(key)
            children = parents
        steps.append(dict(key='root', children=children, title=node.name, node_id=topic_id or domain,
            input_tokens=SUMMARY_INPUT_BUDGET, reuse_id=None))
    if len(steps) > MAX_STEPS:
        raise ApiError(409, 'plan_too_large', '加工批次过多，请缩小知识域范围')
    plan = dict(rules=RULES, generation_revision=GENERATION_REVISION, manifest=manifest.model_dump(),
        topic_id=topic_id, steps=steps, coverage=all_coverage)
    # A ready subtree can be reused independently of unrelated changed batches.
    dummy = SimpleNamespace(id=str(namespace), owner_id=owner, domain_id=domain, scope=scope)
    known = {s['key']: s for s in steps}
    for step in steps:
        if step['children'] and all(known[k]['reuse_id'] for k in step['children']):
            frozen = materialize_step(db, dummy, plan, step)
            step['reuse_id'] = reusable_artifact(db, owner, domain, scope, frozen)
    return plan


def materialize_summary_parent(db, build, plan, step, identifier):
    from .domain_inputs import freeze_domain_inputs
    from .models import DomainSummaryArtifact
    by_key = {s['key']: s for s in plan['steps']}
    documents, child_ids, dependencies = [], [], set()
    for key in step['children']:
        child = by_key[key]
        aid = child['reuse_id'] or str(uuid5(UUID(build.id), key))
        artifact = db.get(DomainSummaryArtifact, aid)
        if artifact is None or (artifact.owner_id, artifact.domain_id, artifact.scope) != (build.owner_id, build.domain_id, build.scope):
            raise ApiError(409, 'missing_child', '汇总批次不存在或范围不符')
        frozen = json.loads(artifact.snapshot_json)
        request = DomainSummaryRequest.model_validate(frozen['request'])
        response = DomainSummaryResponse.model_validate_json(artifact.response_json)
        from .domain_contract import validate_domain_summary
        if response.request_id != request.request_id:
            raise ApiError(409, 'invalid_child', '汇总批次请求编号不符')
        validate_domain_summary(request, response.result)
        points = [response.result.overview, *response.result.insights, *response.result.limitations]
        references = {c for p in points for c in p.citations}
        originals = {e.id: e for e in request.evidence if e.id in references}
        documents.append((DomainContextSummary(node_id=aid, kind='topic',
            text='\n'.join(p.text for p in points), citations=sorted(originals),
            source_ids=sorted({e.source_id for e in originals.values()})), originals))
        child_ids.append(aid)
        dependencies.update(d['source_id'] for d in frozen['manifest']['sources'])
    try:
        request = request_for(identifier, step['node_id'], step['title'], documents,
            kind='topic' if plan['topic_id'] else 'domain', purpose='overview' if step['key'] == 'root' else 'batch')
    except ValueError as error:
        raise ApiError(409, 'parent_budget_exceeded', '批次汇总输入超过已确认预算，未发送') from error
    return dict(request=request.model_dump(), manifest=freeze_domain_inputs(db, build.owner_id, build.domain_id,
        scope=build.scope, allowed_source_ids=frozenset(dependencies)).model_dump(),
        coverage=plan['coverage'], child_artifact_ids=child_ids)
