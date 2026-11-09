"""Persistent topic aliases and bounded processing-node reads, without jobs."""
import json

from sqlalchemy import case, func

from .domain_contract import DomainSummaryRequest, DomainSummaryResponse, validate_domain_summary
from .domain_inputs import freeze_domain_inputs, manifest_fingerprint
from .domain_processing import DomainInputManifest
from .domain_runtime import locked_state
from .domain_topics import source_scope
from .errors import ApiError
from .models import DomainSummaryArtifact


def logical_node_id(artifact):
    snapshot = case((func.json_valid(artifact.snapshot_json) == 1, artifact.snapshot_json), else_='{}')
    return func.coalesce(func.json_extract(snapshot, '$.published_topic_id'), artifact.node_id)


def validate_processing(db, owner, domain, scope, frozen, response, validation_context=None):
    from .domain_retrieval import resolve_original
    request = DomainSummaryRequest.model_validate(frozen['request'])
    manifest = DomainInputManifest.model_validate(frozen['processing_manifest'])
    if (manifest.owner_id, manifest.domain_id, manifest.scope) != (owner, domain, scope) or response.request_id != request.request_id:
        raise ApiError(409, 'snapshot_binding_mismatch', '加工节点范围不符')
    validate_domain_summary(request, response.result)
    if validation_context:
        validation_context.check()
    else:
        locked_state(db, owner, domain, scope)
    ids = frozenset(d.source_id for d in manifest.sources)
    current = (validation_context.manifest(owner, domain, scope, allowed_source_ids=ids) if validation_context
               else freeze_domain_inputs(db, owner, domain, scope=scope, allowed_source_ids=ids))
    if not ids or manifest_fingerprint(manifest) != manifest_fingerprint(current):
        raise ApiError(409, 'stale_inputs', '加工节点输入已变化')
    for entry in request.evidence:
        if entry.source_id not in ids or resolve_original(db, owner, entry, validation_context.query_scope if validation_context else source_scope(owner, domain, scope),
                                                   validation_context=validation_context) is None:
            raise ApiError(409, 'stale_inputs', '加工节点原文已变化')


def processing_view(db, owner, domain, scope, identifier):
    locked_state(db, owner, domain, scope)
    artifact = db.get(DomainSummaryArtifact, identifier)
    if artifact is None or (artifact.owner_id, artifact.domain_id, artifact.scope) != (owner, domain, scope):
        raise ApiError(404, 'not_found', '加工节点不存在')
    try:
        frozen = json.loads(artifact.snapshot_json)
        response = DomainSummaryResponse.model_validate_json(artifact.response_json)
        request = DomainSummaryRequest.model_validate(frozen['request'])
        if response.request_id != artifact.id or request.node_id != artifact.node_id:
            raise ValueError('Artifact binding mismatch')
        coverage = frozen['coverage']
        child_ids = frozen.get('child_artifact_ids', [])
        if not isinstance(coverage, dict) or not isinstance(child_ids, list) or any(not isinstance(i, str) for i in child_ids):
            raise ValueError('Invalid processing metadata')
        if len(set(child_ids)) != len(child_ids):
            raise ValueError('Duplicate processing children')
        if frozen.get('published_topic_id') or frozen.get('step') == 'root':
            from .domain_builds import validate_build_artifact
            validate_build_artifact(db, owner, domain, scope, frozen, response)
        else:
            validate_processing(db, owner, domain, scope, frozen, response)
    except (ApiError, ValueError, KeyError, TypeError):
        return dict(available=False, reason='stale_or_unsupported_inputs')
    children = []
    for child_id in child_ids:
        child = db.get(DomainSummaryArtifact, child_id)
        # Only direct metadata; no recursive loading or expanding access scope.
        if child_id == artifact.id or child is None or (child.owner_id, child.domain_id, child.scope) != (owner, domain, scope):
            return dict(available=False, reason='invalid_children')
        children.append(dict(artifact_id=child.id, node_id=child.node_id))
    return dict(available=True, artifact_id=artifact.id, node_id=artifact.node_id,
        published_topic_id=frozen.get('published_topic_id'), result=response.result.model_dump(),
        coverage=coverage, children=children, original_evidence_required=True)
