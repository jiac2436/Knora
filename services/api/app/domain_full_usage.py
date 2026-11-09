"""Best-effort provider receipts for one completed library update, never estimates."""
import json
from uuid import UUID, uuid5

from sqlalchemy import select

from .domain_query_snapshot import input_batches
from .models import AIArtifact, AIJob, DomainBuild, DomainSummaryArtifact


def _tokens(raw):
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    pair = value.get('prompt_tokens'), value.get('completion_tokens')
    # Older gateways substituted zero for missing receipts. Do not present that
    # placeholder as measured usage or silently omit a paid checkpoint.
    if any(type(n) is not int or n <= 0 for n in pair):
        return None
    return pair


def reported_usage(db, job, documents):
    if job.status != 'completed':
        return None
    receipts = []
    if any(d['status'] == 'completed' and not d.get('job_id') for d in documents):
        return None
    ids = [d['job_id'] for d in documents if d['status'] == 'completed' and d.get('job_id')]
    for batch in input_batches(ids):
        rows = list(db.execute(select(AIJob, AIArtifact).join(AIArtifact, AIArtifact.job_id == AIJob.id)
            .where(AIJob.id.in_(batch), AIJob.owner_id == job.owner_id, AIArtifact.owner_id == job.owner_id)))
        if len(rows) != len(batch):
            return None
        for child, artifact in rows:
            if (child.status != 'completed' or artifact.source_id != child.source_id
                    or json.loads(child.snapshot_json).get('full_job_id') != job.id):
                return None
            # Long-document final artifacts already aggregate segment + merge
            # receipts, so AIJobStep usage must not be counted again here.
            receipts.append(_tokens(artifact.usage_json))
    build = db.get(DomainBuild, job.build_id) if job.build_id else None
    if not build or build.status != 'completed':
        return None
    plan = json.loads(build.plan_json)
    if plan.get('full_job_id') != job.id:
        return None
    fresh = [str(uuid5(UUID(build.id), step['key'])) for step in plan['steps'] if not step.get('reuse_id')]
    for batch in input_batches(fresh):
        rows = list(db.scalars(select(DomainSummaryArtifact).where(DomainSummaryArtifact.id.in_(batch),
            DomainSummaryArtifact.owner_id == job.owner_id, DomainSummaryArtifact.domain_id == job.domain_id,
            DomainSummaryArtifact.scope == 'app')))
        if len(rows) != len(batch):
            return None
        receipts.extend(_tokens(json.loads(row.response_json).get('usage')) for row in rows)
    if any(receipt is None for receipt in receipts):
        return None
    incoming = sum(pair[0] for pair in receipts)
    outgoing = sum(pair[1] for pair in receipts)
    return dict(input_tokens=incoming, output_tokens=outgoing, total_tokens=incoming + outgoing)
