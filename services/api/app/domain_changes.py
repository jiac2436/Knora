"""SQLite input invalidation in the same transaction as business changes."""
from __future__ import annotations

from .database import Base

PREFIX = "pka_domain_"


def _mark(domain: str, owner: str, condition: str = "1", shared: str = "1") -> str:
    return f"""INSERT INTO domain_processing_states
        (domain_id, scope, owner_id, generation, dirty_since, last_changed_at, phase,
         first_build_confirmed, used_calls, used_input_tokens)
        SELECT d.id, scopes.scope, d.owner_id, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
               'not_built', 0, 0, 0
        FROM domains d CROSS JOIN (SELECT 'app' AS scope UNION ALL SELECT 'shareable') scopes
        WHERE d.id = {domain} AND d.owner_id = {owner} AND ({condition})
              AND (scopes.scope = 'app' OR ({shared}))
        ON CONFLICT(domain_id, scope) DO UPDATE SET generation = generation + 1,
        dirty_since = COALESCE(dirty_since, CURRENT_TIMESTAMP), last_changed_at = CURRENT_TIMESTAMP;
        """


def expected_input_triggers() -> dict[str, str]:
    result = {}
    # These are model inputs, not operational jobs/indexes or generated domain output.
    tables = ("sources", "source_revisions", "evidence_chunks", "personal_context",
              "experience_records", "tag_assignments", "source_projects", "ai_artifacts")
    for table in tables:
        for event in ("INSERT", "UPDATE", "DELETE"):
            row = "OLD" if event == "DELETE" else "NEW"
            if table == "sources":
                source = f"{row}.id"
            elif table == "evidence_chunks":
                source = f"(SELECT source_id FROM source_revisions WHERE id = {row}.revision_id)"
            else:
                source = f"{row}.source_id"
            domain = f"(SELECT domain_id FROM sources WHERE id = {source})"
            owner = f"(SELECT owner_id FROM sources WHERE id = {source})"
            if table == "sources":
                domain, owner = f"{row}.domain_id", f"{row}.owner_id"
            gate = "1"
            if event == "UPDATE":
                ignored = {"created_at", "updated_at"}
                if table == "sources":
                    ignored |= {"upload_status", "processing_status", "captured_at", "client_kind", "client_version"}
                if table == "ai_artifacts":
                    ignored |= {"applied_at", "usage_json", "provider", "model", "prompt_version"}
                columns = [c.name for c in Base.metadata.tables[table].columns if c.name not in ignored]
                gate = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in columns)
                if table == "sources":
                    gate += " OR (OLD.processing_status IS NOT NEW.processing_status AND (OLD.processing_status IN ('deleting','deleted') OR NEW.processing_status IN ('deleting','deleted')))"
            shared = f"(SELECT visibility FROM sources WHERE id = {source}) = 'app_and_agents'"
            if table == "sources":
                shared = f"{row}.visibility = 'app_and_agents'"
                if event == "UPDATE":
                    shared = "OLD.visibility = 'app_and_agents' OR NEW.visibility = 'app_and_agents'"
            body = _mark(domain, owner, shared=shared)
            if table == "sources" and event == "UPDATE":
                body += _mark("OLD.domain_id", "OLD.owner_id", "OLD.domain_id IS NOT NEW.domain_id OR OLD.owner_id IS NOT NEW.owner_id", shared)
            elif event == "UPDATE":
                old_source = ("(SELECT source_id FROM source_revisions WHERE id = OLD.revision_id)"
                              if table == "evidence_chunks" else "OLD.source_id")
                body += _mark(f"(SELECT domain_id FROM sources WHERE id = {old_source})",
                              f"(SELECT owner_id FROM sources WHERE id = {old_source})",
                              f"{old_source} IS NOT {source}",
                              f"(SELECT visibility FROM sources WHERE id = {old_source}) = 'app_and_agents'")
            name = f"{PREFIX}{table}_{event.lower()}"
            result[name] = f"CREATE TRIGGER {name} AFTER {event} ON {table} WHEN ({gate}) BEGIN {body} END"
    result[PREFIX + "domain_name"] = f"""CREATE TRIGGER {PREFIX}domain_name AFTER UPDATE ON domains
        WHEN OLD.name IS NOT NEW.name BEGIN {_mark('NEW.id', 'NEW.owner_id')} END"""
    for event in ("UPDATE", "DELETE"):
        gate = "OLD.name IS NOT NEW.name OR OLD.dimension IS NOT NEW.dimension OR OLD.parent_id IS NOT NEW.parent_id" if event == "UPDATE" else "1"
        body = """UPDATE domain_processing_states SET generation = generation + 1,
            dirty_since = COALESCE(dirty_since, CURRENT_TIMESTAMP), last_changed_at = CURRENT_TIMESTAMP
            WHERE domain_id IN (SELECT s.domain_id FROM sources s JOIN tag_assignments a ON a.source_id = s.id
                                WHERE a.tag_id = OLD.id AND (scope = 'app' OR s.visibility = 'app_and_agents'))"""
        name = PREFIX + "tag_" + event.lower()
        result[name] = f"CREATE TRIGGER {name} BEFORE {event} ON knowledge_tags WHEN ({gate}) BEGIN {body}; END"
    result[PREFIX + "domain_delete"] = f"""CREATE TRIGGER {PREFIX}domain_delete BEFORE DELETE ON domains BEGIN
        DELETE FROM domain_processing_states WHERE domain_id = OLD.id;
        DELETE FROM domain_processing_preferences WHERE domain_id = OLD.id; END"""
    return result
