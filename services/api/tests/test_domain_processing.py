"""Domain input isolation and dispatch safety; no real model calls."""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.domain_inputs import dependencies_are_current, freeze_domain_inputs, manifest_fingerprint
from app.domain_processing import DomainBudgetUsage, DomainPendingWork, DomainProcessingPolicy
from app.domain_processing import dispatch_reason
from app.errors import ApiError
from app.models import ExperienceRecord, KnowledgeTag, PersonalContext, Source, utcnow
from app.models import TagAssignment
from test_ai_analysis import storage, create, complete


NOW = datetime(2026, 10, 9, 8, tzinfo=timezone.utc)


def policy(**changes):
    return DomainProcessingPolicy(enabled=True, max_calls=4, max_input_tokens=8000, **changes)


def work(**changes):
    return DomainPendingWork(generation=1, phase="dirty", dirty_since=NOW - timedelta(seconds=60),
                            last_changed_at=NOW - timedelta(seconds=31), **changes)


def test_default_policy_cannot_authorize_paid_processing():
    assert dispatch_reason(DomainProcessingPolicy(), DomainBudgetUsage(), work(), NOW,
                           input_tokens=1000, manual=True) == "disabled"


@pytest.mark.parametrize("phase,manual,confirmed,expected", [
    ("not_built", False, False, "first_build_requires_confirmation"),
    ("not_built", True, False, "ready"),
    ("unknown", False, True, "unknown_requires_confirmation"),
    ("unknown", True, False, "unknown_requires_confirmation"),
    ("unknown", True, True, "ready"),
    ("failed", False, False, "failed_requires_manual_retry"),
    ("failed", True, False, "ready"),
    ("preparing", True, True, "running"),
    ("generating", True, True, "running"),
    ("indexing", True, True, "running"),
    ("ready", False, False, "no_changes"),
])
def test_dispatch_phase_guards(phase, manual, confirmed, expected):
    pending = DomainPendingWork(generation=1, phase=phase)
    assert dispatch_reason(policy(), DomainBudgetUsage(), pending, NOW, input_tokens=1000,
        manual=manual, confirmed_unknown=confirmed) == expected


def test_pause_does_not_grant_budget_and_manual_update_bypasses_pause():
    assert dispatch_reason(policy(paused=True), DomainBudgetUsage(), work(), NOW,
                           input_tokens=1000) == "paused"
    assert dispatch_reason(policy(paused=True), DomainBudgetUsage(), work(), NOW,
                           input_tokens=1000, manual=True) == "ready"
    assert dispatch_reason(policy(paused=True), DomainBudgetUsage(calls=4), work(), NOW,
                           input_tokens=1000, manual=True) == "budget_exhausted"


@pytest.mark.parametrize("calls,tokens,estimate,expected", [
    (3, 7000, 1000, "ready"), (4, 0, 1000, "budget_exhausted"),
    (0, 7001, 1000, "budget_exhausted"), (0, 0, 8001, "budget_exhausted"),
])
def test_budget_counts_reserved_requests(calls, tokens, estimate, expected):
    assert dispatch_reason(policy(), DomainBudgetUsage(calls=calls, input_tokens=tokens), work(), NOW,
                           input_tokens=estimate) == expected


def test_debounce_coalesces_changes_but_cannot_starve_forever():
    pending = DomainPendingWork(generation=1, phase="dirty", dirty_since=NOW - timedelta(seconds=299),
                               last_changed_at=NOW - timedelta(seconds=1))
    assert dispatch_reason(policy(), DomainBudgetUsage(), pending, NOW, input_tokens=1000) == "debouncing"
    assert dispatch_reason(policy(), DomainBudgetUsage(), pending, NOW + timedelta(seconds=1), input_tokens=1000) == "ready"
    assert dispatch_reason(policy(), DomainBudgetUsage(), pending, NOW, input_tokens=1000, manual=True) == "ready"
    assert dispatch_reason(policy(), DomainBudgetUsage(), pending, NOW - timedelta(seconds=2), input_tokens=1000) == "clock_before_change"


@pytest.mark.parametrize("estimate", [0, -1, True, 1.2])
def test_cannot_dispatch_without_conservative_token_estimate(estimate):
    with pytest.raises(ValueError):
        dispatch_reason(policy(), DomainBudgetUsage(), work(), NOW, input_tokens=estimate)


def test_invalid_change_times_and_policy_are_rejected():
    with pytest.raises(ValidationError):
        DomainPendingWork(generation=1, phase="dirty", dirty_since=NOW)
    with pytest.raises(ValidationError):
        DomainPendingWork(generation=1, phase="dirty", dirty_since=NOW,
                          last_changed_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValidationError):
        DomainPendingWork(generation=1, phase="dirty", dirty_since=NOW.replace(tzinfo=None),
                          last_changed_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError):
        DomainProcessingPolicy(debounce_seconds=31, max_wait_seconds=30)


def freeze(data, **kwargs):
    return freeze_domain_inputs(data[0], data[6].owner_id, data[2].domain_id, **kwargs)


def test_manifest_is_read_only_and_owner_domain_isolated(storage):
    db, _, source, _, target, _, principal, other = storage
    initial = freeze(storage)
    assert initial.sources[0].source_id == source.id
    assert initial.sources[0].summary_artifact_id is None
    assert not db.new and not db.dirty
    assert freeze_domain_inputs(db, principal.owner_id, target.id).sources == []
    with pytest.raises(ApiError) as error:
        freeze_domain_inputs(db, other.owner_id, source.domain_id)
    assert error.value.status_code == 404
    assert "Synthetic evidence" not in initial.model_dump_json()
    assert dependencies_are_current(initial, freeze(storage))


def test_permissions_apply_before_manifest_and_private_changes_do_not_dirty_shared_scope(storage):
    db, _, source, *_ = storage
    source.visibility = "app_only"; db.commit()
    shared = freeze(storage, scope="shareable")
    assert shared.sources == []
    assert len(freeze(storage).sources) == 1
    source.title = "Private title edited"; db.commit()
    assert manifest_fingerprint(shared) == manifest_fingerprint(freeze(storage, scope="shareable"))
    assert not dependencies_are_current(freeze(storage), shared)
    assert freeze(storage, allowed_source_ids=frozenset()).sources == []
    assert freeze(storage, allowed_source_ids=frozenset({source.id})).sources[0].source_id == source.id


@pytest.mark.parametrize("change", ["title", "text", "evidence", "context", "experience", "visibility", "tag", "domain_name"])
def test_input_changes_invalidate_manifest_without_revision_change(storage, change):
    db, _, source, chunk, *_ = storage
    before = freeze(storage)
    if change == "title": source.title = "Changed"
    elif change == "text": source.revisions[0].extracted_text = "Edited in place"
    elif change == "evidence": chunk.text = "Edited original evidence"
    elif change == "context": db.add(PersonalContext(source_id=source.id, notes="Personal practice"))
    elif change == "experience": db.add(ExperienceRecord(owner_id=source.owner_id, source_id=source.id, result="success", notes="Verified practice"))
    elif change == "visibility": source.visibility = "app_only"
    elif change == "domain_name":
        from app.models import Domain
        db.get(Domain, source.domain_id).name = "Changed domain"
    else:
        tag = KnowledgeTag(owner_id=source.owner_id, dimension="topic", name="Architecture", normalized_name="architecture")
        db.add(tag); db.flush()
        db.add(TagAssignment(source_id=source.id, tag_id=tag.id, origin="manual"))
    db.commit()
    after = freeze(storage)
    assert before.sources[0].revision_id == after.sources[0].revision_id
    assert manifest_fingerprint(before) != manifest_fingerprint(after)
    assert not dependencies_are_current(before, after)


def test_progress_and_output_index_changes_do_not_change_inputs(storage):
    db, _, source, *_ = storage
    before = freeze(storage)
    source.updated_at = utcnow(); source.upload_status = "uploading"
    db.commit()
    assert manifest_fingerprint(before) == manifest_fingerprint(freeze(storage))


@pytest.mark.parametrize("change", ["delete", "move", "revoke", "new_source"])
def test_domain_membership_changes_invalidate_published_content(storage, change):
    db, _, source, _, target, *_ = storage
    before = freeze(storage, scope="shareable")
    if change == "delete": source.deleted_at = utcnow()
    elif change == "move": source.domain_id = target.id
    elif change == "revoke": source.visibility = "app_only"
    else: db.add(Source(owner_id=source.owner_id, domain_id=source.domain_id, kind="text", title="New", coverage="full_text"))
    db.commit()
    after = freeze_domain_inputs(db, before.owner_id, before.domain_id, scope="shareable")
    assert not dependencies_are_current(before, after)
    if change != "new_source": assert after.sources == []


def test_current_summary_publication_is_input_but_stale_summary_is_excluded(storage):
    db, _, source, *_ = storage
    before = freeze(storage)
    create(storage, kind="analyze"); complete(storage)
    after = freeze(storage)
    assert after.sources[0].summary_artifact_id is not None
    assert manifest_fingerprint(before) != manifest_fingerprint(after)
    source.title = "Changed after analysis"; db.commit()
    assert freeze(storage).sources[0].summary_artifact_id is None


def test_empty_manifest_and_unknown_scope_cannot_supply_generated_facts(storage):
    empty = freeze(storage, allowed_source_ids=frozenset())
    assert not dependencies_are_current(empty, empty)
    with pytest.raises(ValueError): freeze(storage, scope="invalid")
