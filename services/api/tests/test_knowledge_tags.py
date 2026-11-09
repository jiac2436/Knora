"""Vocabulary and provenance behavior using synthetic local records."""
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app import knowledge_tags as tags
from app.auth import AppPrincipal
from app.database import Base, make_engine
from app.errors import ApiError
from app.models import Domain, KnowledgeTag, Source, SourceRevision, SourceTag, TagAssignment, User
from app.services import source_view
from app.tags import replace_auto_tags


@pytest.fixture
def storage(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'tags.db'}")
    with engine.connect() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)() as db:
        owner = User(email="tags@example.test", name="Owner")
        other = User(email="other@example.test", name="Other")
        db.add_all([owner, other]); db.flush()
        domain = Domain(owner_id=owner.id, name="Default")
        db.add(domain); db.flush()
        source = Source(owner_id=owner.id, domain_id=domain.id, title="AI人工智能", kind="text", coverage="full_text")
        db.add(source); db.flush()
        revision = SourceRevision(source_id=source.id, version=1, extracted_text="人工智能")
        db.add(revision); db.flush()
        source.current_revision_id = revision.id
        db.commit()
        yield db, source, AppPrincipal(owner.id), AppPrincipal(other.id)
    engine.dispose()


def create(data, name="人工智能", dimension="topic", parent_id=None):
    db, _, owner, _ = data
    return tags.create_tag(tags.CreateTag(name=name, dimension=dimension, parent_id=parent_id), owner, db)


def test_normalization_alias_reuse_and_namespace(storage):
    db, _, owner, _ = storage
    tag = create(storage, "  ＡＩ  ")
    assert tag["name"] == "AI"
    assert create(storage, "ai")["id"] == tag["id"]
    tags.add_alias(tag["id"], tags.AliasRequest(name="人工智能"), owner, db)
    assert create(storage)["id"] == tag["id"]
    assert create(storage, "ai", "entity")["id"] != tag["id"]
    conflicting = create(storage, "机器学习")
    with pytest.raises(ApiError) as exc:
        tags.add_alias(conflicting["id"], tags.AliasRequest(name="AI"), owner, db)
    assert exc.value.status_code == 409
    db.rollback()
    with pytest.raises(ApiError):
        tags.patch_tag(conflicting["id"], tags.PatchTag(name="人工智能"), owner, db)


def test_two_level_topics_and_no_cycles(storage):
    db, _, owner, _ = storage
    parent = create(storage)
    child = create(storage, "RAG", parent_id=parent["id"])
    for name, dimension, parent_id in [("三级", "topic", child["id"]), ("实体", "entity", parent["id"])]:
        with pytest.raises(ApiError):
            create(storage, name, dimension, parent_id)
        db.rollback()
    with pytest.raises(ApiError):
        tags.patch_tag(parent["id"], tags.PatchTag(parent_id=child["id"]), owner, db)
    db.rollback()
    with pytest.raises(ApiError):
        tags.merge_tag(parent["id"], tags.MergeRequest(target_id=child["id"]), owner, db)


def test_dual_origin_manual_priority_and_stale(storage):
    db, source, owner, _ = storage
    tag = create(storage)
    db.add(TagAssignment(source_id=source.id, tag_id=tag["id"], origin="ai", revision_id=source.current_revision_id))
    db.commit()
    body = tags.AssignmentRequest(tag_id=tag["id"])
    tags.assign_tag(source.id, body, owner, db)
    tags.assign_tag(source.id, body, owner, db)
    assert len(db.scalars(select(TagAssignment)).all()) == 2
    revision = SourceRevision(source_id=source.id, version=2, extracted_text="Updated")
    db.add(revision); db.flush(); source.current_revision_id = revision.id; db.commit()
    view = tags.source_tags(source.id, owner, db)
    item = view["tag_details"][0]
    assert set(item["origins"]) == {"manual", "ai"} and not item["stale"]
    assert item["assignments"][0]["stale"]
    tags.unassign_tag(source.id, tag["id"], "manual", owner, db)
    view = tags.source_tags(source.id, owner, db)
    assert view["tag_details"][0]["stale"] and view["tags"] == []
    tags.assign_tag(source.id, body, owner, db)
    tags.unassign_tag(source.id, tag["id"], "ai", owner, db)
    assert tags.source_tags(source.id, owner, db)["tag_details"][0]["origins"] == ["manual"]


def test_rules_do_not_replace_formal_tags(storage):
    db, source, owner, _ = storage
    tag = create(storage, "我自己的标签")
    tags.assign_tag(source.id, tags.AssignmentRequest(tag_id=tag["id"]), owner, db)
    replace_auto_tags(db, source); db.commit()
    view = source_view(db, source)
    assert "我自己的标签" in view["tags"]
    assert "人工智能" in view["legacy_rule_tags"]
    assert view["tag_details"][0]["origins"] == ["manual"]


def test_owner_and_deleted_source_isolation(storage):
    db, source, owner, other = storage
    tag = create(storage)
    assert tags.list_tags(None, 50, 0, other, db)["items"] == []
    for action in [lambda: tags.source_tags(source.id, other, db),
        lambda: tags.assign_tag(source.id, tags.AssignmentRequest(tag_id=tag["id"]), other, db),
        lambda: tags.patch_tag(tag["id"], tags.PatchTag(name="X"), other, db)]:
        with pytest.raises(ApiError) as exc:
            action()
        assert exc.value.status_code == 404
        db.rollback()
    from app.models import utcnow
    source.deleted_at = utcnow(); db.commit()
    with pytest.raises(ApiError):
        tags.assign_tag(source.id, tags.AssignmentRequest(tag_id=tag["id"]), owner, db)


def test_merge_preserves_origins_aliases_and_children(storage):
    db, source, owner, _ = storage
    old, target = create(storage, "AI"), create(storage, "人工智能")
    child = create(storage, "RAG", parent_id=old["id"])
    tags.add_alias(old["id"], tags.AliasRequest(name="人工智慧"), owner, db)
    tags.assign_tag(source.id, tags.AssignmentRequest(tag_id=old["id"]), owner, db)
    db.add(TagAssignment(source_id=source.id, tag_id=old["id"], origin="ai", revision_id=source.current_revision_id))
    db.add(TagAssignment(source_id=source.id, tag_id=target["id"], origin="ai", revision_id=str(uuid4())))
    db.commit()
    result = tags.merge_tag(old["id"], tags.MergeRequest(target_id=target["id"]), owner, db)
    assert {item["name"] for item in result["aliases"]} == {"AI", "人工智慧"}
    assert db.get(KnowledgeTag, child["id"]).parent_id == target["id"]
    assert db.get(KnowledgeTag, old["id"]) is None
    view = tags.source_tags(source.id, owner, db)["tag_details"][0]
    assert set(view["origins"]) == {"manual", "ai"}
    assert not any(item["stale"] for item in view["assignments"])
    assert create(storage, "AI")["id"] == target["id"]


def test_pagination_and_alias_removal(storage):
    db, _, owner, _ = storage
    first = create(storage, "A")
    create(storage, "B")
    result = tags.list_tags(None, 1, 0, owner, db)
    assert len(result["items"]) == 1 and result["next_offset"] == 1
    added = tags.add_alias(first["id"], tags.AliasRequest(name="Alpha"), owner, db)
    assert tags.remove_alias(first["id"], added["aliases"][0]["id"], owner, db)["aliases"] == []


@pytest.mark.parametrize("name", [" ", "\x00", "bad\x01name"])
def test_invalid_names(storage, name):
    with pytest.raises(ApiError):
        create(storage, name)


def test_http_contract_rejects_spoofed_origin_and_lists_detail(client, account):
    from conftest import capture_payload
    headers = account["headers"]
    source = client.post("/v1/captures", headers={**headers, "Idempotency-Key": str(uuid4())},
        json=capture_payload(account["domain"]["id"]))
    assert source.status_code == 201
    source_id = source.json()["id"]
    tag = client.post("/v1/tags", headers=headers, json={"name": "归档", "dimension": "type"})
    assert tag.status_code == 200
    tag_id = tag.json()["id"]
    path = f"/v1/sources/{source_id}/tags"
    assert client.post(path, headers=headers, json={"tag_id": tag_id, "origin": "ai"}).status_code == 422
    result = client.post(path, headers=headers, json={"tag_id": tag_id})
    assert result.status_code == 200
    assert result.json()["tag_details"][0]["origins"] == ["manual"]
    assert client.get(path, headers=headers).json()["tags"] == ["归档"]
