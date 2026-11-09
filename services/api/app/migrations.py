"""Local SQLite migrations: explicit preparation, backup and transactional DDL."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import uuid

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from .database import Base
from .platform_support import get_platform

CURRENT_VERSION = 16
DOMAIN_FULL_ANALYSIS_TABLES = frozenset({"domain_full_analysis_jobs"})
DOMAIN_NODE_INDEX_TABLES = frozenset({"domain_node_index_jobs", "domain_node_embeddings"})
DOMAIN_BUILD_TABLES = frozenset({"domain_builds"})
DOMAIN_SUMMARY_TABLES = frozenset({"domain_summary_jobs", "domain_summary_artifacts"})
DOMAIN_TOPIC_TABLES = frozenset({'domain_topics', 'domain_topic_members', 'domain_topic_index_states'})
DOMAIN_PROCESSING_TABLES = frozenset({'domain_processing_preferences', 'domain_processing_states'})
RAG_TABLES = frozenset({'rag_sessions', 'rag_turns', 'rag_jobs'})
AI_TABLES = frozenset({'ai_jobs', 'ai_artifacts'})
TAG_TABLES = frozenset({'knowledge_tags', 'knowledge_tag_aliases', 'tag_assignments'})
TAG_ANALYSIS_TABLES = frozenset({'tag_suggestions', 'tag_rejections'})
AI_STEP_TABLES = frozenset({'ai_job_steps'})
SUMMARY_INDEX_TABLES = frozenset({'summary_index_jobs', 'summary_search_terms', 'summary_embeddings'})


def apply_legacy_columns(connection: Connection) -> None:
    additions = [
        ("ocr_preferences", "engine", "VARCHAR(20) NOT NULL DEFAULT 'pdfium'"),
        ("ocr_jobs", "engine", "VARCHAR(20) NOT NULL DEFAULT 'pdfium'"),
        ("web_read_jobs", "engine", "VARCHAR(20) NOT NULL DEFAULT 'legacy'"),
        ("web_read_preferences", "cleaning_enabled", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("web_read_jobs", "cleaning_enabled", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ("source_images", "retrieval_method", "VARCHAR(20) NOT NULL DEFAULT 'download'"),
    ]
    for table, column, declaration in additions:
        columns = {item["name"] for item in inspect(connection).get_columns(table)}
        if column not in columns:
            clause = "IF NOT EXISTS " if connection.dialect.name == "postgresql" else ""
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {clause}{column} {declaration}"))


def baseline(connection: Connection) -> None:
    from . import models  # noqa: F401
    Base.metadata.create_all(connection)
    apply_legacy_columns(connection)


def local_change_journal(connection: Connection) -> None:
    from .local_changes import SYNC_TABLES, initialize_journal, install_change_triggers
    from . import models  # noqa: F401
    for name in sorted(SYNC_TABLES):
        Base.metadata.tables[name].create(connection, checkfirst=True)
    install_change_triggers(connection, version=2)
    identity = connection.execute(text("SELECT library_id, device_id FROM local_installations WHERE id = 1")).first()
    if identity is not None:
        initialize_journal(connection, identity.library_id, identity.device_id, version=2)


def local_ai(connection: Connection) -> None:
    from .local_changes import install_change_triggers, validate_change_triggers
    validate_change_triggers(connection, version=2)
    for name in sorted(AI_TABLES):
        Base.metadata.tables[name].create(connection, checkfirst=True)
    install_change_triggers(connection, version=3)


def knowledge_tags(connection: Connection) -> None:
    from .local_changes import install_change_triggers, validate_change_triggers
    validate_change_triggers(connection, version=3)
    for name in sorted(TAG_TABLES):
        Base.metadata.tables[name].create(connection, checkfirst=True)
    install_change_triggers(connection, version=4)


def tag_analysis(connection: Connection) -> None:
    from .local_changes import install_change_triggers, validate_change_triggers
    validate_change_triggers(connection, version=4)
    for name in sorted(TAG_ANALYSIS_TABLES):
        Base.metadata.tables[name].create(connection, checkfirst=True)
    install_change_triggers(connection, version=5)


def long_ai_checkpoints(connection: Connection) -> None:
    from .local_changes import validate_change_triggers
    validate_change_triggers(connection, version=5)
    Base.metadata.tables['ai_job_steps'].create(connection, checkfirst=True)


def summary_indexes(connection: Connection) -> None:
    from .local_changes import validate_change_triggers
    validate_change_triggers(connection, version=6)
    for name in sorted(SUMMARY_INDEX_TABLES):
        Base.metadata.tables[name].create(connection, checkfirst=True)


def local_rag(connection: Connection) -> None:
    from .local_changes import install_change_triggers, validate_change_triggers
    validate_change_triggers(connection, version=7)
    for name in ('rag_sessions', 'rag_turns', 'rag_jobs'):
        Base.metadata.tables[name].create(connection, checkfirst=True)
    install_change_triggers(connection, version=8)


def domain_processing(connection: Connection) -> None:
    from .local_changes import install_change_triggers, validate_change_triggers
    validate_change_triggers(connection, version=8)
    for name in sorted(DOMAIN_PROCESSING_TABLES):
        Base.metadata.tables[name].create(connection, checkfirst=True)
    install_change_triggers(connection, version=9)
    connection.exec_driver_sql("""INSERT INTO domain_processing_states
        (domain_id, scope, owner_id, generation, dirty_since, last_changed_at, phase,
         first_build_confirmed, used_calls, used_input_tokens)
        SELECT d.id, scopes.scope, d.owner_id, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
               'not_built', 0, 0, 0 FROM domains d
        CROSS JOIN (SELECT 'app' AS scope UNION ALL SELECT 'shareable') scopes
        WHERE 1 ON CONFLICT(domain_id, scope) DO NOTHING""")


def domain_topics(connection: Connection) -> None:
    from .local_changes import install_change_triggers, validate_change_triggers
    validate_change_triggers(connection, version=9)
    for name in ('domain_topics', 'domain_topic_members', 'domain_topic_index_states'):
        Base.metadata.tables[name].create(connection, checkfirst=True)
    install_change_triggers(connection, version=10)


def domain_summaries(connection: Connection) -> None:
    from .local_changes import install_change_triggers, validate_change_triggers
    validate_change_triggers(connection, version=10)
    for name in ('domain_summary_jobs', 'domain_summary_artifacts'):
        Base.metadata.tables[name].create(connection, checkfirst=True)
    install_change_triggers(connection, version=11)


def domain_builds(connection: Connection) -> None:
    from .local_changes import install_change_triggers, validate_change_triggers
    validate_change_triggers(connection, version=11)
    Base.metadata.tables['domain_builds'].create(connection, checkfirst=True)
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_node_step_key ON domain_summary_artifacts "
        "(owner_id, domain_id, scope, json_extract(snapshot_json, '$.step_key'))")
    install_change_triggers(connection, version=12)


def domain_node_indexes(connection: Connection) -> None:
    from .local_changes import install_change_triggers, validate_change_triggers
    validate_change_triggers(connection, version=12)
    for name in sorted(DOMAIN_NODE_INDEX_TABLES):
        Base.metadata.tables[name].create(connection, checkfirst=True)
    install_change_triggers(connection, version=13)


def compact_source_search_index(connection: Connection) -> None:
    """Normalize only the rebuildable cache; keep all valid legacy tokens."""
    from .local_changes import validate_change_triggers
    validate_change_triggers(connection, version=13)
    columns = {item['name'] for item in inspect(connection).get_columns('source_search_terms')}
    Base.metadata.tables['source_search_documents'].create(connection, checkfirst=True)
    if 'document_id' in columns:
        return  # A fresh baseline already uses the current cache layout.
    connection.exec_driver_sql('ALTER TABLE source_search_terms RENAME TO source_search_terms_legacy')
    Base.metadata.tables['source_search_terms'].create(connection)
    # An inconsistent cached Owner invalidates the whole document, including its
    # completion marker. Startup backfill will rebuild it from the real Source.
    connection.exec_driver_sql('''INSERT INTO source_search_documents (source_id, owner_id)
        SELECT DISTINCT s.id, s.owner_id FROM source_search_terms_legacy t JOIN sources s ON s.id=t.source_id
        WHERE NOT EXISTS (SELECT 1 FROM source_search_terms_legacy bad
            WHERE bad.source_id=s.id AND bad.owner_id IS NOT s.owner_id)''')
    connection.exec_driver_sql('''INSERT INTO source_search_terms (document_id, term)
        SELECT d.id, t.term FROM source_search_terms_legacy t
        JOIN source_search_documents d ON d.source_id=t.source_id''')
    connection.exec_driver_sql('DROP TABLE source_search_terms_legacy')
    # SQLite otherwise tends to choose the Owner-first join for large caches.
    connection.exec_driver_sql('ANALYZE source_search_documents')
    connection.exec_driver_sql('ANALYZE source_search_terms')


def domain_full_analysis(connection: Connection) -> None:
    from .local_changes import install_change_triggers, validate_change_triggers
    validate_change_triggers(connection, version=14)
    Base.metadata.tables['domain_full_analysis_jobs'].create(connection, checkfirst=True)
    install_change_triggers(connection, version=15)


def domain_trash(connection: Connection) -> None:
    from .local_changes import install_change_triggers
    Base.metadata.tables["domain_trash"].create(connection, checkfirst=True)
    install_change_triggers(connection, version=16)


MIGRATIONS = {1: ("baseline-and-local-identity", baseline),
              2: ("local-change-journal", local_change_journal),
              3: ("local-ai-jobs-and-artifacts", local_ai),
              4: ("knowledge-tags-and-provenance", knowledge_tags),
              5: ("ai-tag-suggestions-and-rejections", tag_analysis),
              6: ("long-ai-analysis-checkpoints", long_ai_checkpoints),
              7: ("local-summary-search-indexes", summary_indexes),
              8: ("local-rag-sessions-and-turns", local_rag),
              9: ("domain-processing-input-state", domain_processing),
              10: ("domain-local-topics", domain_topics),
              11: ("domain-summary-checkpoints", domain_summaries),
              12: ("domain-bounded-build-plans", domain_builds),
              13: ("domain-node-local-vector-indexes", domain_node_indexes),
              14: ("compact-source-search-cache", compact_source_search_index),
              15: ("domain-full-analysis-workflows", domain_full_analysis),
              16: ("recoverable-domain-deletion", domain_trash)}


def schema_version(connection: Connection) -> int:
    if "schema_migrations" not in inspect(connection).get_table_names():
        return 0
    rows = connection.execute(text("SELECT version, name FROM schema_migrations ORDER BY version")).all()
    for version, name in rows:
        if version not in MIGRATIONS or MIGRATIONS[version][0] != name:
            raise ValueError("数据库版本不受当前客户端支持")
    if [row[0] for row in rows] != list(range(1, len(rows) + 1)):
        raise ValueError("数据库迁移记录不连续")
    return rows[-1][0] if rows else 0


def snapshot(source: Path, destination: Path) -> None:
    """Backup API includes committed WAL data; caller holds the preparation lock."""
    get_platform().files.reserve(destination)
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as reader:
            with closing(sqlite3.connect(destination)) as writer:
                reader.backup(writer)
                if writer.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("数据库备份完整性检查失败")
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def migrate(engine: Engine, backups: Path) -> Path | None:
    if engine.dialect.name != "sqlite" or not engine.url.database or engine.url.database == ":memory:":
        raise ValueError("本地迁移仅支持文件 SQLite")
    with engine.connect() as connection:
        version = schema_version(connection)
        if version == CURRENT_VERSION:
            require_schema(connection)
            return None
    database = Path(engine.url.database)
    backup = None
    if database.stat().st_size:
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = backups / f"pre-migration-{uuid.uuid4()}.sqlite"
        snapshot(database, backup)
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            if schema_version(connection) != version:
                raise ValueError("数据库版本在准备期间发生变化")
            connection.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"))
            for number in range(version + 1, CURRENT_VERSION + 1):
                name, operation = MIGRATIONS[number]
                operation(connection)
                connection.execute(text("INSERT INTO schema_migrations VALUES (:version, :name, CURRENT_TIMESTAMP)"),
                                   {"version": number, "name": name})
            require_schema(connection)
            if connection.exec_driver_sql("PRAGMA integrity_check").scalar() != "ok":
                raise ValueError("迁移后数据库完整性检查失败")
            if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                raise ValueError("迁移后数据库关联完整性检查失败")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    get_platform().files.protect(database)
    return backup


def require_schema(connection: Connection, *, allow_previous: bool = False) -> None:
    version = schema_version(connection)
    if version != CURRENT_VERSION and not (allow_previous and version in range(1, CURRENT_VERSION)):
        raise ValueError("本地数据库尚未准备，请先执行本地初始化")
    from . import models  # noqa: F401
    from .local_changes import SYNC_TABLES, validate_change_triggers
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if version < 16 and table.name == "domain_trash":
            continue
        if version < 15 and table.name in DOMAIN_FULL_ANALYSIS_TABLES:
            continue
        if version < 14 and table.name == 'source_search_documents':
            continue
        if version < 13 and table.name in DOMAIN_NODE_INDEX_TABLES:
            continue
        if version < 12 and table.name in DOMAIN_BUILD_TABLES:
            continue
        if version < 11 and table.name in DOMAIN_SUMMARY_TABLES:
            continue
        if version < 10 and table.name in DOMAIN_TOPIC_TABLES:
            continue
        if version < 9 and table.name in DOMAIN_PROCESSING_TABLES:
            continue
        if version < 8 and table.name in RAG_TABLES:
            continue
        if version < 7 and table.name in SUMMARY_INDEX_TABLES:
            continue
        if version < 6 and table.name in AI_STEP_TABLES:
            continue
        if version < 5 and table.name in TAG_ANALYSIS_TABLES:
            continue
        if version < 4 and table.name in TAG_TABLES:
            continue
        if version < 3 and table.name in AI_TABLES:
            continue
        if version == 1 and table.name in SYNC_TABLES:
            continue
        if table.name not in tables:
            raise ValueError("本地数据库结构不完整")
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        expected = ({'source_id', 'owner_id', 'term'} if version < 14 and table.name == 'source_search_terms' and 'document_id' not in columns
                    else {column.name for column in table.columns})
        if not expected.issubset(columns):
            raise ValueError("本地数据库字段不完整")
    if version >= 2:
        validate_change_triggers(connection, version=version)
