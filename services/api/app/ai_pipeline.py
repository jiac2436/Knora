"""Read-only product state for analysis publication and local retrieval indexes."""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AIArtifact, AIJob, Source


def pipeline_view(db: Session, job: AIJob | None = None,
                  artifact: AIArtifact | None = None) -> dict:
    """Model completion alone is not completion of the knowledge workflow."""
    from .summary_index import (artifact_index_status, current_summary_artifact,
                                source_semantic_index_status)

    def view(status: str, index_status: str = "not_started") -> dict:
        return {"pipeline_status": status, "index_state": index_status,
                "index_retryable": status in {"indexing", "index_failed"}}

    if artifact is None and job is not None:
        if job.status in {"pending", "running"}:
            return view("analyzing")
        if job.status in {"failed", "unknown", "cancelled"}:
            return view(job.status)
        candidates = db.scalars(select(AIArtifact).where(AIArtifact.job_id == job.id,
            AIArtifact.owner_id == job.owner_id, AIArtifact.source_id == job.source_id)
            .order_by(AIArtifact.created_at.desc(), AIArtifact.id.desc()))
        for candidate in candidates:
            try:
                snapshot = json.loads(candidate.snapshot_json)
                wire = snapshot.get("analysis_request", snapshot)
                if snapshot.get("phase", "single") != "segment" and wire.get("phase", "single") != "segment":
                    artifact = candidate
                    break
            except (ValueError, TypeError, AttributeError):
                continue
    if artifact is None:
        return view("failed")
    source = db.get(Source, artifact.source_id)
    if source is None or source.owner_id != artifact.owner_id:
        return view("stale")
    current = current_summary_artifact(db, source)
    if current is None or current.id != artifact.id:
        return view("stale")
    summary_status = artifact_index_status(db, artifact)
    original_status = source_semantic_index_status(db, source)
    if "failed" in {summary_status, original_status}:
        return view("index_failed", "failed")
    if summary_status != "ready" or original_status not in {"ready", "disabled"}:
        return view("indexing", "pending")
    return view("completed", "ready")
