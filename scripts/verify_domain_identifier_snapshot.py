"""Disposable snapshot diagnostics; local BGE only, never a model gateway.

Scope subsets reuse the 1000-source snapshot; they do not rebuild the original
20/100/300 experiments with their different document lengths and UUIDs.
"""
import argparse
import cProfile
from datetime import datetime, timezone
import functools
import hashlib
import io
import json
import os
from pathlib import Path
import pstats
import re
import shutil
import sqlite3
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profile', action='store_true', help='Two queries plus storage diagnostics only')
    args = parser.parse_args()
    runtime = args.runtime.resolve(strict=True)
    snapshot = args.snapshot.resolve(strict=True)
    original_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    report = dict(started_at_utc=datetime.now(timezone.utc).isoformat(), runtime=str(runtime.relative_to(Path(__file__).resolve().parents[1])) if runtime.is_relative_to(Path(__file__).resolve().parents[1]) else runtime.name,
        snapshot_sha256=original_hash, runtime_manifest_sha256=hashlib.sha256((runtime / 'manifest.json').read_bytes()).hexdigest(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), scope='fixed 1000-source snapshot subsets, not rebuilt scale experiments',
        llm_calls=0, real_user_data_used=False)
    with tempfile.TemporaryDirectory(prefix='pka-scale-diagnostic.') as temporary:
        root = Path(temporary).resolve()
        os.environ.update(PKA_ENVIRONMENT='local', PKA_DATA_DIR=str(root / 'data'),
            PKA_CACHE_DIR=str(root / 'cache'), PKA_LOG_DIR=str(root / 'logs'),
            PKA_AI_GATEWAY_URL='', PKA_AI_GATEWAY_TOKEN='', PKA_SEMANTIC_ENABLED='1',
            PKA_SEMANTIC_MODEL_PATH=str(runtime / 'model'), HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
        (root / 'data').mkdir()
        target = root / 'data/knowledge.db'
        shutil.copyfile(snapshot, target)
        sys.path.insert(0, str(runtime / 'backend'))
        from sqlalchemy import event, select
        from app import semantic, services, summary_retrieval, domain_retrieval, domain_summary_inputs
        from app.database import engine, SessionLocal
        from app.config import settings
        from app.models import Source, Project, SourceProject
        from app.schemas import SearchRequest
        from app.migrations import CURRENT_VERSION, migrate, schema_version
        assert settings.ai_gateway_url == '' and settings.ai_gateway_token == ''
        assert Path(settings.local_paths.database) == target
        with sqlite3.connect(f'file:{snapshot}?mode=ro', uri=True) as connection:
            names = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            counts = {name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in names}
            report['storage'] = dict(sqlite_bytes=snapshot.stat().st_size, table_rows=counts,
                source_title_characters=connection.execute('SELECT sum(length(title)) FROM sources').fetchone()[0],
                revision_text_characters=connection.execute('SELECT sum(length(extracted_text)) FROM source_revisions').fetchone()[0],
                evidence_text_characters=connection.execute('SELECT sum(length(text)) FROM evidence_chunks').fetchone()[0],
                evidence_windows=counts.get('evidence_embeddings'),
                vector_payload_bytes={name: connection.execute('SELECT sum(length(vector)) FROM ' + name).fetchone()[0]
                    for name in ('evidence_embeddings', 'summary_embeddings', 'domain_node_embeddings')})
            try:
                report['storage']['dbstat_bytes'] = {r[0]: r[1] for r in connection.execute('SELECT name,sum(pgsize) FROM dbstat GROUP BY name ORDER BY sum(pgsize) DESC')}
            except sqlite3.OperationalError as error:
                report['storage']['dbstat_unavailable'] = str(error)
        # Upgrade only the disposable clone; the retained v13 snapshot is read-only.
        with engine.connect() as connection:
            old_version = schema_version(connection)
        migration_started = time.perf_counter()
        backup = migrate(engine, settings.local_paths.backups)
        with engine.connect() as connection:
            new_version = schema_version(connection)
        assert new_version == CURRENT_VERSION
        migration = dict(old_schema_version=old_version, new_schema_version=new_version,
            current_schema_version=CURRENT_VERSION,
            elapsed_ms=(time.perf_counter() - migration_started) * 1000,
            pre_migration_backup_created=backup is not None, automatic_vacuum=False)
        if backup is not None:
            with sqlite3.connect(f'file:{backup}?mode=ro', uri=True) as connection:
                backup_version = connection.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
                backup_source_count = connection.execute('SELECT count(*) FROM sources').fetchone()[0]
            assert backup_version == old_version
            assert backup_source_count == report['storage']['table_rows']['sources']
            migration.update(pre_migration_backup_schema_version=backup_version,
                pre_migration_backup_source_count=backup_source_count,
                pre_migration_backup_sha256=hashlib.sha256(backup.read_bytes()).hexdigest())
        elif old_version != new_version:
            raise AssertionError('Existing database upgrade must create a pre-migration backup')
        report['migration'] = migration
        with sqlite3.connect(f'file:{target}?mode=ro', uri=True) as connection:
            names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            post_storage = dict(physical_database_bytes=target.stat().st_size,
                physical_wal_bytes=Path(str(target) + '-wal').stat().st_size if Path(str(target) + '-wal').exists() else 0,
                physical_shm_bytes=Path(str(target) + '-shm').stat().st_size if Path(str(target) + '-shm').exists() else 0,
                page_size=connection.execute('PRAGMA page_size').fetchone()[0],
                page_count=connection.execute('PRAGMA page_count').fetchone()[0],
                free_page_count=connection.execute('PRAGMA freelist_count').fetchone()[0],
                table_rows={name: connection.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in names},
                limits='Physical file includes reusable free pages; migration does not run VACUUM. Fresh-layout size must be measured on a newly built library.')
            try:
                post_storage['dbstat_bytes'] = {row[0]: row[1] for row in connection.execute('SELECT name,sum(pgsize) FROM dbstat GROUP BY name ORDER BY sum(pgsize) DESC')}
            except sqlite3.OperationalError as error:
                post_storage['dbstat_unavailable'] = str(error)
            post_storage['free_page_bytes'] = post_storage['page_size'] * post_storage['free_page_count']
        report['post_migration_storage'] = post_storage
        print(f'MIGRATION {old_version} -> {new_version}; backup={backup is not None}; free pages={post_storage["free_page_count"]}', flush=True)
        semantic._model()
        with SessionLocal() as db:
            sources = list(db.scalars(select(Source)))
            owner = sources[0].owner_id
            numbers = {s.id: int(re.search(r'Q(\d+)', s.title).group(1)) for s in sources}
            if args.profile:
                accumulated = {}
                def instrument(module, name):
                    original = getattr(module, name)
                    @functools.wraps(original)
                    def wrapped(*a, **k):
                        start = time.perf_counter()
                        try:
                            return original(*a, **k)
                        finally:
                            entry = accumulated.setdefault(module.__name__ + '.' + name, dict(calls=0, milliseconds=0))
                            entry['calls'] += 1
                            entry['milliseconds'] += (time.perf_counter() - start) * 1000
                    setattr(module, name, wrapped)
                for module, name in ((semantic, 'semantic_hits'), (semantic, 'embed_query'),
                    (summary_retrieval, 'summary_hits'), (summary_retrieval, 'tag_hits'),
                    (domain_retrieval, 'node_hits'), (domain_retrieval, 'current_node_result'),
                    (domain_retrieval, 'resolve_original'), (domain_summary_inputs, 'freeze_domain_inputs')):
                    instrument(module, name)
                sql = dict(count=0, milliseconds=0)
                @event.listens_for(engine, 'before_cursor_execute')
                def before(conn, cursor, statement, params, context, many):
                    context.scale_started = time.perf_counter()
                @event.listens_for(engine, 'after_cursor_execute')
                def after(conn, cursor, statement, params, context, many):
                    sql['count'] += 1
                    sql['milliseconds'] += (time.perf_counter() - context.scale_started) * 1000
                profiles = []
                for query in ('策略Q0000', '系统断电之后怎样避免已确认写入丢失'):
                    accumulated.clear(); sql.update(count=0, milliseconds=0)
                    profiler = cProfile.Profile()
                    start = time.perf_counter()
                    profiler.enable()
                    rows = services.search_knowledge(db, owner, SearchRequest(query=query, limit=10))
                    profiler.disable()
                    elapsed = (time.perf_counter() - start) * 1000
                    out = io.StringIO()
                    pstats.Stats(profiler, stream=out).sort_stats('cumulative').print_stats(30)
                    profiles.append(dict(query=query, elapsed_ms=elapsed, channels=json.loads(json.dumps(accumulated)),
                        sql=dict(sql), source_numbers=[numbers[r['source_id']] for r in rows], cprofile=out.getvalue().replace(str(Path(__file__).resolve().parents[1]), '<repository>').replace(str(root), '<temporary>')))
                    print(f'PROFILE {query}: {elapsed:.1f} ms, SQL {sql["count"]}', flush=True)
                report['profiles'] = profiles
                report['profile_limits'] = 'Two queries with instrumentation overhead; inclusive nested durations cannot be summed; not an idle benchmark.'
            else:
                originals = (summary_retrieval.summary_hits, domain_retrieval.node_hits)
                stages = []
                for size in (20, 100, 300, 1000):
                    project = Project(owner_id=owner, name=f'合成快照范围{size}')
                    db.add(project); db.flush()
                    db.add_all(SourceProject(source_id=s.id, project_id=project.id) for s in sources if numbers[s.id] < size)
                    db.commit()
                    for label in ('original_only', 'original_and_authored_summaries', 'with_authored_topics'):
                        summary_retrieval.summary_hits = originals[0] if label != 'original_only' else lambda *a, **k: ({}, {})
                        domain_retrieval.node_hits = originals[1] if label == 'with_authored_topics' else lambda *a, **k: {}
                        answers = []
                        for number in range(10):
                            query = f'策略Q{number:04d}'
                            rows = services.search_knowledge(db, owner, SearchRequest(query=query, project_id=project.id, limit=10))
                            found = [numbers[r['source_id']] for r in rows]
                            answers.append(dict(query=query, expected=number, source_numbers=found, hit=number in found, top1_hit=bool(found and found[0] == number),
                                match_sources=[r['match_sources'] for r in rows]))
                        stages.append(dict(scope_size=size, label=label, detail_hit_at_10=sum(a['hit'] for a in answers)/10, descriptive_top1_hit=sum(a['top1_hit'] for a in answers)/10, answers=answers))
                        print(f'SCOPE {size} {label}: {stages[-1]["detail_hit_at_10"]}', flush=True)
                summary_retrieval.summary_hits, domain_retrieval.node_hits = originals
                report['comparisons'] = stages
        engine.dispose()
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == original_hash, 'retained snapshot changed'
    report['completed_at_utc'] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
