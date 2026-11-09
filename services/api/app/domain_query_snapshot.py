"""Request-local validation inputs for already permission-gated SQLite nodes.

Never retain this object beyond node_hits. Cached input hashes remain valid only
inside the same locked transaction, with no intervening ORM or SQL mutation.
"""
import json
from collections import defaultdict

from sqlalchemy import select

from .ai import context_view
from .domain_inputs import _digest, freeze_domain_inputs
from .domain_processing import DomainInputManifest
from .domain_topics import source_scope
from .errors import ApiError
from .summary_retrieval import source_tags_many
from .models import (AIArtifact, Domain, DomainTopic, DomainTopicMember, EvidenceChunk,
                     ExperienceRecord, PersonalContext, Project, Source, SourceProject, SourceRevision)


INPUT_BATCH_SIZE = 500


def input_batches(identifiers):
    values = list(identifiers)
    for offset in range(0, len(values), INPUT_BATCH_SIZE):
        yield values[offset:offset + INPUT_BATCH_SIZE]


class NodeInputRows:
    """Batch reads for the unchanged freeze_domain_inputs payload contract."""
    def __init__(self, snapshot):
        self.snapshot = snapshot
        db, owner, sources = snapshot.db, snapshot.owner, snapshot.sources
        ids = list(sources)
        self.contexts = {row.source_id: row for batch in input_batches(ids)
            for row in db.scalars(select(PersonalContext).where(PersonalContext.source_id.in_(batch)))}
        self.experiences = defaultdict(list)
        for batch in input_batches(ids):
            for row in db.execute(select(ExperienceRecord.id, ExperienceRecord.project_id, ExperienceRecord.result,
                    ExperienceRecord.notes, ExperienceRecord.verified_at, ExperienceRecord.source_id).where(
                    ExperienceRecord.owner_id == owner, ExperienceRecord.source_id.in_(batch)).order_by(ExperienceRecord.id)):
                self.experiences[row.source_id].append(row)
        self.project_ids, self.projects = defaultdict(list), defaultdict(list)
        self.tags, self.candidates = {}, defaultdict(list)
        for batch in input_batches(ids):
            for source_id, project_id in db.execute(select(SourceProject.source_id, SourceProject.project_id)
                    .where(SourceProject.source_id.in_(batch)).order_by(SourceProject.project_id)):
                self.project_ids[source_id].append(project_id)
            for source_id, project in db.execute(select(SourceProject.source_id, Project).join(Project,
                    Project.id == SourceProject.project_id).where(SourceProject.source_id.in_(batch)).order_by(Project.id)):
                self.projects[source_id].append(project)
            self.tags.update(source_tags_many(db, owner, batch))
            for artifact in db.scalars(select(AIArtifact).join(Source, AIArtifact.source_id == Source.id).where(
                    Source.id.in_(batch), AIArtifact.owner_id == owner, AIArtifact.revision_id == Source.current_revision_id)
                    .order_by(AIArtifact.created_at.desc(), AIArtifact.id.desc())):
                self.candidates[artifact.source_id].append(artifact)
        self.revisions = {row.id: row for row in snapshot.revisions}
        self.evidence_rows = defaultdict(list)
        for row in sorted(snapshot.evidence, key=lambda row: (row.ordinal, row.id)):
            self.evidence_rows[row.revision_id].append((row.id, row.ordinal, row.text, row.locator_kind, row.locator_start, row.locator_end))

    def check_source(self, db, source):
        self.snapshot.check()
        if db is not self.snapshot.db or self.snapshot.sources.get(source.id) is not source:
            raise ApiError(409, 'query_snapshot_scope', '预加载资料不属于本次检索')

    def revision(self, source):
        revision = self.revisions.get(source.current_revision_id)
        return revision if revision and revision.source_id == source.id else None

    def context_state(self, source):
        return context_view(source, self.contexts.get(source.id), self.snapshot.domains.get((source.domain_id, 'app'))
            or self.snapshot.domains.get((source.domain_id, 'shareable')), self.projects.get(source.id, []))


class NodeQuerySnapshot:
    def __init__(self, db, owner, query_scope, artifacts):
        self.db, self.owner, self.query_scope = db, owner, query_scope
        self.transaction = db.get_transaction()
        if self.transaction is None:
            raise ApiError(409, 'query_snapshot_changed', '检索快照需要已锁定事务')
        self.connection = db.connection().connection.driver_connection
        self.changes = self.connection.total_changes
        self.check()
        self.artifact_ids = {row.id for row in artifacts}
        self.inputs, self.domains, self.topics, self.members, self.live_ids = {}, {}, {}, {}, {}
        groups = {}
        for artifact in artifacts:
            frozen = json.loads(artifact.snapshot_json)
            ids = groups.setdefault((artifact.domain_id, artifact.scope), set())
            for name in ('manifest', 'global_manifest', 'topic_manifest', 'processing_manifest'):
                ids.update(entry['source_id'] for entry in frozen.get(name, {}).get('sources', []))
            ids.update(entry['source_id'] for entry in frozen['request']['evidence'])
        universe = set().union(*groups.values()) if groups else set()
        self.sources = {row.id: row for batch in input_batches(sorted(universe))
                        for row in db.scalars(query_scope.where(Source.id.in_(batch)))}
        if set(self.sources) != universe:
            raise ApiError(409, 'query_snapshot_scope', '检索依赖范围已变化')
        revisions = {row.current_revision_id for row in self.sources.values() if row.current_revision_id}
        # Strong references keep db.get in evidence validation from reloading
        # the same rows after each node; every reference still compares text.
        self.revisions = [row for batch in input_batches(sorted(revisions))
                          for row in db.scalars(select(SourceRevision).where(SourceRevision.id.in_(batch)))]
        self.evidence = [row for batch in input_batches(sorted(revisions))
                        for row in db.scalars(select(EvidenceChunk).where(EvidenceChunk.revision_id.in_(batch)))]
        for (domain, scope), ids in groups.items():
            self.check()
            key = (domain, scope)
            self.domains[key] = db.scalar(select(Domain).where(Domain.id == domain, Domain.owner_id == owner))
            self.topics[key] = {row.id: row for row in db.scalars(select(DomainTopic).where(
                DomainTopic.owner_id == owner, DomainTopic.domain_id == domain, DomainTopic.scope == scope))}
            topic_ids = list(self.topics[key])
            self.members[key] = [row for batch in input_batches(topic_ids)
                for row in db.execute(select(DomainTopicMember.topic_id, DomainTopicMember.source_id,
                    DomainTopicMember.included, DomainTopicMember.manual).where(DomainTopicMember.topic_id.in_(batch)))]
            self.live_ids[key] = frozenset(db.scalars(source_scope(owner, domain, scope).with_only_columns(Source.id)))
        self.rows = NodeInputRows(self)
        for (domain, scope), ids in groups.items():
            self.inputs[(domain, scope)] = freeze_domain_inputs(db, owner, domain, scope=scope,
                allowed_source_ids=frozenset(ids), prefetched=self.rows)
        self.check()

    def check(self):
        if (self.transaction is None or self.db.get_transaction() is not self.transaction or not self.transaction.is_active
                or self.db.new or self.db.dirty or self.db.deleted
                or self.connection.total_changes != self.changes):
            raise ApiError(409, 'query_snapshot_changed', '当前检索事务发生变化，请重新检索')

    def domain(self, owner, domain, scope):
        self.check()
        row = self.domains.get((domain, scope))
        if owner != self.owner or row is None or row.owner_id != owner:
            raise ApiError(409, 'query_snapshot_scope', '检索快照范围不符')
        return row

    def topic(self, owner, domain, scope, topic_id):
        self.domain(owner, domain, scope)
        row = self.topics[(domain, scope)].get(topic_id)
        if row is None:
            raise ApiError(404, 'not_found', '主题不存在')
        return row

    def node_source_ids(self, owner, domain, scope, topic_id=None):
        self.domain(owner, domain, scope)
        available = self.live_ids[(domain, scope)]
        if topic_id is None:
            return available
        self.topic(owner, domain, scope, topic_id)
        return frozenset(row.source_id for row in self.members[(domain, scope)]
                         if row.topic_id == topic_id and row.included and row.source_id in available)

    def manifest(self, owner, domain, scope, *, topic_id=None, allowed_source_ids=None):
        self.domain(owner, domain, scope)
        ids = self.node_source_ids(owner, domain, scope, topic_id) if allowed_source_ids is None else allowed_source_ids
        base = self.inputs[(domain, scope)]
        dependencies = {item.source_id: item for item in base.sources}
        # A new member may be visible but absent from the gated artifact union.
        # It invalidates that node; do not read its body to expand this snapshot.
        if not ids.issubset(dependencies):
            raise ApiError(409, 'query_snapshot_incomplete', '知识域成员已变化，请更新概览')
        topics = self.topics[(domain, scope)].values()
        manual = sorted((row.topic_id, row.source_id, row.included) for row in self.members[(domain, scope)]
                        if row.manual and row.source_id in ids)
        manual_topics = {row[0] for row in manual}
        structure = dict(topics=[[row.id, row.name, row.parent_id, row.name_fixed, row.automatic_key]
            for row in sorted(topics, key=lambda row: row.id)
            if row.name_fixed or row.parent_id or row.automatic_key is None or row.id in manual_topics],
            members=[list(row) for row in manual])
        return DomainInputManifest(owner_id=owner, domain_id=domain, domain_name=base.domain_name,
            scope=scope, sources=[dependencies[identifier] for identifier in sorted(ids)], structure_sha256=_digest(structure))

    def source(self, owner, identifier, query_scope):
        self.check()
        if owner != self.owner or query_scope is not self.query_scope:
            raise ApiError(409, 'query_snapshot_scope', '原文检索范围不符')
        return self.sources.get(identifier)
