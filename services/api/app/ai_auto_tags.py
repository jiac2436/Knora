"""Atomic publication of machine labels; manual labels and user corrections survive."""
from __future__ import annotations

import json

from sqlalchemy import delete
from sqlalchemy.orm import Session

from .ai_tag_suggestions import rejection_matches
from .knowledge_tags import normalize, resolve_name
from .models import AIArtifact, KnowledgeTag, Source, TagAssignment, TagSuggestion, utcnow


def publish_tags(db: Session, source: Source, artifact: AIArtifact, candidates) -> None:
    """Caller holds source and vocabulary locks and commits artifact, labels and index together."""
    assignments = {}
    for candidate in candidates:
        name, key = normalize(candidate.name)
        tag = db.get(KnowledgeTag, candidate.tag_id) if candidate.tag_id else resolve_name(
            db, source.owner_id, candidate.dimension, key)
        if rejection_matches(db, source, candidate.dimension, name, candidate.tag_id):
            db.add(TagSuggestion(owner_id=source.owner_id, source_id=source.id, artifact_id=artifact.id,
                revision_id=artifact.revision_id, tag_id=candidate.tag_id, name=name, normalized_name=key,
                dimension=candidate.dimension, reason=candidate.reason,
                evidence_json=json.dumps(candidate.citations), status="rejected", decided_at=utcnow()))
            continue
        if tag is None:
            if candidate.dimension == "type":
                continue
            tag = KnowledgeTag(owner_id=source.owner_id, dimension=candidate.dimension,
                name=name, normalized_name=key)
            db.add(tag); db.flush()
        if tag.owner_id != source.owner_id or tag.dimension != candidate.dimension:
            raise ValueError("Tag ownership changed")
        assignments[tag.id] = candidate
        db.add(TagSuggestion(owner_id=source.owner_id, source_id=source.id, artifact_id=artifact.id,
            revision_id=artifact.revision_id, tag_id=tag.id, name=tag.name, normalized_name=tag.normalized_name,
            dimension=tag.dimension, reason=candidate.reason,
            evidence_json=json.dumps(candidate.citations), status="accepted", decided_at=utcnow()))
    # A successful new analysis replaces only machine associations, never manual ones.
    db.execute(delete(TagAssignment).where(TagAssignment.source_id == source.id,
        TagAssignment.origin == "ai"))
    db.flush()
    for tag_id, candidate in assignments.items():
        db.add(TagAssignment(source_id=source.id, tag_id=tag_id, origin="ai",
            revision_id=artifact.revision_id, artifact_id=artifact.id,
            evidence_json=json.dumps(candidate.citations, ensure_ascii=False),
            # Existing schema requires this timestamp. For AI it means publication,
            # not human confirmation; origin remains the authoritative distinction.
            confirmed_at=utcnow()))
    db.flush()


def catalog_unchanged(db: Session, snapshot: dict, owner_id: str, candidates) -> bool:
    """Do not silently publish responses against renamed/deleted vocabulary."""
    for candidate in candidates:
        if candidate.tag_id:
            tag = db.get(KnowledgeTag, candidate.tag_id)
            frozen = next((t for t in snapshot.get("tags", []) if t["id"] == candidate.tag_id), None)
            if (tag is None or frozen is None or tag.owner_id != owner_id
                    or tag.name != frozen["name"] or tag.dimension != frozen["dimension"]):
                return False
    return True
