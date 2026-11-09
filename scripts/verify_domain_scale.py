"""Frozen scale-v1: real packaged BGE, disposable synthetic libraries, zero LLM calls."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import tempfile
import time
import traceback
from uuid import uuid4

VERSION = 'scale-v1'
SIZES = (20, 100, 300, 1000)
GATES = dict(detail_hit_at_10=.90, semantic_topic_precision_at_10=.70,
             query_p95_ms=2000, indexing_seconds=1800, peak_rss_mib=2048,
             sqlite_total_mib=256, auxiliary_preserves_detail=1.0)
# Each row has separate factual, limitation and verification text. This is a
# fixture generator, not a claim that the facts form a human-reviewed corpus.
FACTS = [
    ('SQLite恢复', '事务提交后日志需要落盘，启动恢复先回放WAL，再校验主库和外键。',
     '仅复制主数据库文件会遗漏仍在日志里的已确认写入。', '系统断电之后怎样避免已确认写入丢失'),
    ('缓存失效', '更新数据库成功后使缓存失效，读取按资料版本校验缓存，旧值不得冒充当前结果。',
     '并发读取可能在删除缓存后重新写入旧值，需要版本号与重试边界。', '并发写入后怎样阻止缓存把旧值重新放回'),
    ('备份恢复', '导出使用一致性数据库快照，恢复先检查清单和校验和，在隔离目录完成验证。',
     '同步副本不能替代有历史版本的备份，误删除可能同步到全部设备。', '同步时误删文件为什么还需要历史备份'),
    ('检索证据', '摘要只用于召回，回答具体事实时读取当前原文片段，展示版本和引用位置。',
     '向量接近只表示候选相关，不能把摘要里推断出的结论当作原文事实。', '搜索得到相近摘要后如何确认事实出处'),
    ('地址访问', '请求网页前解析域名并禁止私有地址，连接和重定向阶段重新校验目标。',
     '只验证最初URL不足以抵御跳转或DNS重新绑定，预览也要遵守地址限制。', '网页跳转到内部地址时如何防止服务器访问内网'),
    ('Agent授权', '每个代理使用独立只读凭证，查询在授权知识域和可分享资料集合内完成。',
     '不能先读取完整私有概览再过滤结果，也不能用另一代理凭证绕过撤销。', '外部助手检索时如何避免读取未授权资料'),
    ('长文分段', '长文按有界原文片段分析，各段保留准确偏移，合成只引用可回溯的原文。',
     '分段完成不等于全文已经完整分析，缺失段落必须显示覆盖范围。', '文章太长分段处理时怎样保留证据和覆盖范围'),
    ('未知计费', '请求发出后断网时标记结果未知，保留预算预留，不在进程重启后自动重发。',
     '服务端可能已经完成请求，盲目重试既可能重复计费也可能重复写入产物。', '模型调用断网后为什么不能自动再次发送'),
    ('离线同步', '离线修改记录本地版本，同步时提交基线与变更，云端排序并检测过期基线。',
     '中心端保证到达顺序不等于解决离线编辑冲突，不可直接覆盖另一设备修改。', '两台设备离线修改同一文档应该怎样处理冲突'),
    ('应用签名', '客户端凭证存在钥匙串，更新包保留稳定签名身份并核验运行时资源清单。',
     '无签名或变化的开发构建可能触发重新授权，不能用明文文件替代凭证保护。', '应用更新之后怎样避免钥匙串反复要求授权'),
]


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(part)
    return value.hexdigest()


def corpus(size):
    count = 6 if size <= 100 else 8 if size == 300 else 10
    rows = []
    for number in range(size):
        name, fact, caution, question = FACTS[number % len(FACTS)]
        marker = f'策略Q{number:04d}'
        project = f'计划{number // 10:03d}'
        retention, retries, batch = 7 + number % 23, 1 + number % 4, 8 + number % 17
        paragraphs = [
            f'{marker} 属于{project}的{name}设计记录，编号{number}。核心业务规则：{fact} '
            f'此次验证以资料版本{1 + number % 5}为基线，明确区分检索候选、当前原文和用户确认后的操作。',
            f'适用前提：运行在本地工作站，所有写操作在一次短事务中完成。{caution} '
            f'操作{number}的负责人先记录输入清单，再检查版本、可见范围和配置来源；检查失败则停止，不自动扩大访问范围。',
            f'执行策略：{fact} 本轮允许最多{retries}次人工确认后的新操作，每批处理{batch}条，历史记录保存{retention}天。'
            f'这些参数只是{project}的测试约束，不能当作所有项目的默认生产配置。不同条件需要重新评估并在变更清单中说明。',
            f'反例和边界：{caution} 项目{project}曾观察到{number % 13 + 1}条输入缺少必要元信息，不能把它们混入成功数量。'
            f'在资料缺失、引用失效或授权撤销时返回明确状态；已有成功步骤保留，失败步骤不伪装成正常结果。',
            f'验证方法：针对{name}构造正常、失败、重启三组用例，逐项检查{fact} '
            f'记录处理前后的内容哈希、资料版本、实际请求次数和数据库完整性，不仅看界面是否显示完成。每组固定{batch}个样本，结果保留到审查结束。',
            f'运行观察：测试序号{number}的空闲耗时预算为{100 + number % 51}毫秒，后台维护时间为{retention}秒。'
            f'这是合成观察值，不是测量数据，也不代表{name}已达到性能门槛。实际耗时必须单独计时，未完成的索引不能计为向量就绪。'
            f'对照案例要求同一批输入与同一问题集，不能为了改善结果临时修改分组或删除困难样本。',
            f'变更管理：{project}的上一版关注最低可用流程，本版补充{name}的限制条件与回滚说明。'
            f'正文变更后旧摘要只作为历史保存，当前查询重新核验版本。人工备注保留其来源和适用范围，不能被系统归纳覆盖。'
            f'测试编号{number}的修订意见是优先修复可重复出现的错误，避免为单个特例引入新的框架。',
            f'可观测性：记录索引状态、候选来源和结果覆盖，不记录凭证或原文中的个人敏感字段。'
            f'{name}的日志包含安全错误类别和任务编号，方便核对输入、处理和输出是否一致。'
            f'项目{project}的保留策略是{retention}天，过期后删除临时缓存但保留必要的版本依据；导出前再次检查权限。',
            f'风险处置：{caution} 若情况发生在{project}的生产演练中，先暂停相关操作，再确认是否存在已经发出的请求。'
            f'恢复需要重新查看基线、引用与预算，不能仅凭最后一条成功日志推断所有步骤都成功。'
            f'本记录只支持当前{name}问题，不自动给其他项目生成结论；无法解释的数据应标成待核验。',
            f'决策记录：在{batch}个输入的固定验收组中，方案选择遵循{fact} '
            f'保留未选方案的限制：{caution} 计划{project}的后续工作是使用真实业务语料复核阅读价值和问题答案。'
            f'这份编号{number}的模板文档是工程数据，不能用其结果推导真实知识库的准确率或商业可用性。',
        ]
        text = '\n\n'.join(paragraphs[:count])
        rows.append(dict(number=number, facet=number % 10, marker=marker,
            title=f'{marker} {name}', text=text,
            summary=f'{marker}记录{project}的{name}。{fact}限制：{caution}',
            question=question, private=number % 5 == 4))
    return rows


def percentile(values, rank):
    return sorted(values)[max(0, math.ceil(len(values) * rank) - 1)]


def scenario(args):
    runtime = args.runtime.resolve(strict=True)
    sys.path.insert(0, str(runtime / 'backend'))
    from sqlalchemy import func, select
    from app import ai, semantic, services, summary_index, domain_node_index
    from app.ai_contract import AnalysisRequest, AnalysisResult, validate_result
    from app.config import settings
    from app.database import engine, SessionLocal
    from app.domain_contract import DomainSummaryResponse
    from app.domain_summary_inputs import build_node_request, validate_node_publication
    from app.domain_summary_jobs import current_node_result
    from app.local_runtime import initialize_identity
    from app.migrations import migrate, CURRENT_VERSION
    from app.models import (AIArtifact, Domain, DomainSummaryArtifact, DomainTopic, DomainTopicMember,
        EvidenceChunk, EvidenceEmbedding, Project, Source, SourceRevision, SummaryEmbedding,
        DomainNodeEmbedding, SemanticIndexJob, SummaryIndexJob, DomainNodeIndexJob, utcnow)
    from app.schemas import SearchRequest
    started = time.monotonic()
    settings.local_paths.prepare()
    migrate(engine, settings.local_paths.backups)
    identity = initialize_identity(engine, 'synthetic-scale-credential-' + '0' * 24)
    owner = identity['owner_id']
    assert settings.ai_gateway_url == '' and settings.ai_gateway_token == ''
    assert settings.semantic_model_path == str(runtime / 'model') and settings.semantic_enabled
    model_hash = digest(runtime / 'model/model_optimized.onnx')
    cold_started = time.monotonic()
    semantic._model()  # Cold load included in total indexing time, no downloads.
    cold_load_seconds = time.monotonic() - cold_started
    data = corpus(args.scenario)
    corpus_hash = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    ids, indices = [], {}
    durations = {'cold_model_load_seconds': cold_load_seconds}
    evidence_ids = []
    with SessionLocal() as db:
        domain = Domain(owner_id=owner, name='合成工程规模领域')
        empty = Domain(owner_id=owner, name='空白边界领域')
        project = Project(owner_id=owner, name='空白边界项目')
        db.add_all([domain, empty, project]); db.commit()
        originals_started = time.monotonic()
        for row in data:
            source = Source(owner_id=owner, domain_id=domain.id, title=row['title'], kind='text',
                coverage='full_text', processing_status='usable',
                visibility='app_only' if row['private'] else 'app_and_agents')
            db.add(source); db.flush()
            services._create_revision(db, source, row['text'])
            db.flush(); services.reindex_source(db, source)
            db.commit()  # reindex_source already queues its real semantic job.
            ids.append(source.id); indices[source.id] = row['number']
            evidence_ids.append(db.scalar(select(EvidenceChunk.id).where(EvidenceChunk.revision_id == source.current_revision_id)))
        creation_seconds = time.monotonic() - originals_started
        original_vectors_started = time.monotonic()
        count = 0
        while semantic.index_one(db):
            count += 1
            if count % 25 == 0 or count == args.scenario:
                print(f'PROGRESS {args.scenario}: 原文BGE {count}/{args.scenario}', flush=True)
        durations['original_vectors_seconds'] = time.monotonic() - original_vectors_started
        durations['creation_seconds'] = creation_seconds
        for job in db.scalars(select(SemanticIndexJob)):
            assert job.status == 'completed', job.last_error
        assert db.scalar(select(func.count()).select_from(EvidenceEmbedding)) > 0

        questions = [(f'detail-{i}', data[i]['marker'], i, False) for i in range(10)]
        questions += [(f'semantic-{i}', FACTS[i][3], i, True) for i in range(10)]

        def evaluate(label):
            # Warmup excludes inference startup/cache effects from query latency.
            for _, query, _, _ in questions:
                services.search_knowledge(db, owner, SearchRequest(query=query, limit=10))
            samples, answers = [], {}
            for _ in range(3):
                for key, query, target, is_semantic in questions:
                    begin = time.perf_counter()
                    rows = services.search_knowledge(db, owner, SearchRequest(query=query, limit=10))
                    elapsed = (time.perf_counter() - begin) * 1000
                    samples.append(dict(question=key, milliseconds=round(elapsed, 3)))
                    numbers = [indices[item['source_id']] for item in rows]
                    answers[key] = dict(source_numbers=numbers,
                        detail_hit=(target in numbers) if not is_semantic else None,
                        topic_precision=(sum(data[n]['facet'] == target for n in numbers) / len(numbers))
                            if is_semantic and numbers else 0 if is_semantic else None,
                        match_sources=[item['match_sources'] for item in rows])
                    # Never accept a summary/node result as the original quote.
                    for item in rows:
                        chunk = db.get(EvidenceChunk, item['evidence_id'])
                        assert chunk is not None and item['snippet'] in chunk.text
            times = [item['milliseconds'] for item in samples]
            detail = sum(answers[f'detail-{i}']['detail_hit'] for i in range(10)) / 10
            probe = sum(answers[f'semantic-{i}']['topic_precision'] for i in range(10)) / 10
            result = dict(label=label, query_count=len(samples), p50_ms=round(percentile(times, .5), 3),
                p95_ms=round(percentile(times, .95), 3), max_ms=round(max(times), 3),
                detail_hit_at_10=detail, synthetic_topic_precision_at_10=probe,
                answers=answers, latency_samples=samples)
            print(f'PROGRESS {args.scenario}: {label} P95={result["p95_ms"]}ms detail={detail} synthetic_probe={probe}', flush=True)
            return result

        results = [evaluate('original_only')]
        summary_started = time.monotonic()
        for source_id, row in zip(ids, data, strict=True):
            source = db.get(Source, source_id)
            identifier = str(uuid4())
            snapshot = ai.freeze_snapshot(db, source, 'analyze', identifier, 3)
            refs = [snapshot['evidence'][0]['id']]
            response = AnalysisResult(contract_version=3,
                overview={'text': FACTS[row['facet']][0] + '设计记录。', 'citations': refs},
                summary=row['summary'], summary_citations=refs, insights=[], limitations=[],
                organizing_suggestion=None, tag_suggestions=[], warnings=[])
            request = AnalysisRequest.model_validate({key: value for key, value in snapshot.items()
                if key in AnalysisRequest.model_fields})
            validate_result(request, response)
            artifact = AIArtifact(owner_id=owner, source_id=source.id, job_id=identifier,
                revision_id=source.current_revision_id, snapshot_json=ai.canonical(snapshot),
                result_json=response.model_dump_json(), provider='authored-fixture', model='not-an-LLM',
                prompt_version=VERSION, usage_json='{"prompt_tokens":0,"completion_tokens":0}')
            db.add(artifact); db.commit()
            summary_index.enqueue_summary_index(db, source); db.commit()
        count = 0
        while summary_index.index_summary_once(db):
            count += 1
            if count % 25 == 0 or count == args.scenario:
                print(f'PROGRESS {args.scenario}: 摘要fixture BGE {count}/{args.scenario}', flush=True)
        durations['authored_summaries_setup_and_index_seconds'] = time.monotonic() - summary_started
        for job in db.scalars(select(SummaryIndexJob)):
            assert job.status == 'completed', job.last_error
        assert db.scalar(select(func.count()).select_from(SummaryEmbedding)) >= args.scenario
        results.append(evaluate('original_and_authored_summaries'))
        topics_started = time.monotonic()
        topics, artifacts = [], []
        for begin in range(0, args.scenario, 10):
            topic = DomainTopic(owner_id=owner, domain_id=domain.id, scope='app', name=f'人工合成主题{begin // 10}', name_fixed=True)
            db.add(topic); db.flush()
            for source_id in ids[begin:begin + 10]:
                db.add(DomainTopicMember(topic_id=topic.id, source_id=source_id, manual=True))
            db.commit()
            topics.append(topic)
        # Freeze artifacts only after the directory is complete: the directory
        # hash is part of every node's version binding.
        for topic in topics:
            identifier = uuid4()
            frozen = build_node_request(db, owner, domain.id, 'app', identifier, topic.id)
            refs = [item['id'] for item in frozen['request']['evidence'][:10]]
            response = DomainSummaryResponse(request_id=str(identifier), provider='authored-fixture',
                model='not-an-LLM', prompt_version=VERSION, usage={'prompt_tokens': 0, 'completion_tokens': 0},
                result={'overview': {'text': '本组合涵盖恢复、缓存、备份、检索、访问控制、授权、分段、计费、同步与签名。仅作为导航，具体条件请读取原文。', 'citations': refs}})
            validate_node_publication(db, owner, domain.id, 'app', frozen, response)
            db.add(DomainSummaryArtifact(id=str(identifier), owner_id=owner, domain_id=domain.id, scope='app',
                node_id=topic.id, snapshot_json=ai.canonical(frozen), response_json=response.model_dump_json()))
            db.commit(); artifacts.append(str(identifier))
        domain_node_index.ensure_node_index(db)
        count = 0
        while domain_node_index.index_node_once(db):
            count += 1
        assert count == len(topics)
        for job in db.scalars(select(DomainNodeIndexJob)):
            assert job.status == 'completed', job.last_error
        assert db.scalar(select(func.count()).select_from(DomainNodeEmbedding)) >= len(topics)
        durations['authored_topics_setup_and_index_seconds'] = time.monotonic() - topics_started
        results.append(evaluate('with_authored_topics'))
        preserves = all(not results[1]['answers'][f'detail-{i}']['detail_hit']
            or results[2]['answers'][f'detail-{i}']['detail_hit'] for i in range(10))

        security = {}
        security['ungranted_domain_empty'] = not services.search_knowledge(db, owner, SearchRequest(query='合成', limit=10), set(), True)
        security['other_owner_empty'] = not services.search_knowledge(db, 'absent-owner', SearchRequest(query=data[0]['marker'], limit=10))
        security['empty_project_empty'] = not services.search_knowledge(db, owner, SearchRequest(query=data[0]['marker'], project_id=project.id, limit=10))
        security['empty_domain_empty'] = not services.search_knowledge(db, owner, SearchRequest(query=data[0]['marker'], domain_ids=[empty.id], limit=10))
        agent_rows = services.search_knowledge(db, owner, SearchRequest(query='知识检索', limit=20), {domain.id}, True)
        security['agent_no_private_sources'] = all(not data[indices[item['source_id']]]['private'] for item in agent_rows)
        security['agent_no_app_node'] = all('knowledge_node' not in item for item in agent_rows)
        removed = db.get(Source, ids[0]); removed.deleted_at = utcnow(); db.commit()
        deleted_rows = services.search_knowledge(db, owner, SearchRequest(query=data[0]['marker'], limit=10))
        security['deleted_source_absent'] = all(item['source_id'] != ids[0] for item in deleted_rows)
        security['affected_topic_stale'] = not current_node_result(db, owner, domain.id, 'app', topics[0].id)['available']
        changed = db.get(Source, ids[1]); services._create_revision(db, changed, '当前替换正文，仅用于旧证据失效断言。')
        db.flush(); services.reindex_source(db, changed); db.commit()
        revised_rows = services.search_knowledge(db, owner, SearchRequest(query=data[1]['marker'], limit=10))
        security['old_revision_evidence_absent'] = all(item['evidence_id'] != evidence_ids[1] for item in revised_rows)
        with engine.connect() as connection:
            integrity = connection.exec_driver_sql('PRAGMA integrity_check').scalar()
            foreign_keys = connection.exec_driver_sql('PRAGMA foreign_key_check').all()
        counts = {name: db.scalar(select(func.count()).select_from(table)) for name, table in (
            ('evidence_embeddings', EvidenceEmbedding), ('summary_embeddings', SummaryEmbedding), ('node_embeddings', DomainNodeEmbedding))}
        db.commit()
    engine.dispose()
    durations['total_index_seconds'] = sum(durations.values())
    durations['cold_load_and_end_to_end_seconds'] = time.monotonic() - started
    database = settings.local_paths.database
    sqlite_bytes = sum(path.stat().st_size for path in (database, Path(str(database) + '-wal'), Path(str(database) + '-shm')) if path.exists())
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024 if sys.platform == 'darwin' else 1024)
    checks = dict(details=all(result['detail_hit_at_10'] >= GATES['detail_hit_at_10'] for result in results),
        semantic_probe=results[0]['synthetic_topic_precision_at_10'] >= GATES['semantic_topic_precision_at_10'],
        query_latency=all(result['p95_ms'] <= GATES['query_p95_ms'] for result in results),
        indexing_time=durations['total_index_seconds'] <= GATES['indexing_seconds'],
        memory=peak <= GATES['peak_rss_mib'], sqlite_size=sqlite_bytes / 1024 ** 2 <= GATES['sqlite_total_mib'],
        auxiliary_preserves_detail=preserves, security=all(security.values()), integrity=integrity == 'ok' and not foreign_keys)
    record = dict(size=args.scenario, schema_version=CURRENT_VERSION, status='passed' if all(checks.values()) else 'threshold_failed',
        corpus_sha256=corpus_hash, model_sha256=model_hash, source_characters=sum(len(row['text']) for row in data),
        source_utf8_bytes=sum(len(row['text'].encode()) for row in data), min_source_characters=min(len(row['text']) for row in data),
        max_source_characters=max(len(row['text']) for row in data), million_characters_reached=sum(len(row['text']) for row in data) >= 1000000,
        timings=durations, query_comparisons=results, peak_rss_mib=round(peak, 2), sqlite_total_bytes=sqlite_bytes,
        vector_counts=counts, authored_topic_count=len(topics), safety=security, gates=checks,
        llm_calls=0, real_user_data_used=False, semantic_model=semantic.MODEL_NAME,
        real_llm_summary_quality='not_measured', real_theme_quality='not_measured', real_semantic_quality='not_measured',
        reading_value='not_measured', real_rag_answer_quality='not_measured')
    args.stage_output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    print(f'RESULT {args.scenario}: {record["status"]}, RSS={peak:.1f}MiB, chars={record["source_characters"]}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--sizes', nargs='+', type=int, choices=SIZES, default=list(SIZES))
    parser.add_argument('--scenario', type=int, choices=SIZES)
    parser.add_argument('--stage-output', type=Path)
    args = parser.parse_args()
    if args.scenario:
        scenario(args); return
    if args.output is None:
        parser.error('--output required')
    runtime = args.runtime.resolve(strict=True)
    model = runtime / 'model/model_optimized.onnx'
    assert model.is_file(), 'bundled BGE model required; downloads forbidden'
    protocol = Path(__file__).resolve().parents[1] / 'scripts/tests/fixtures/domain-scale-protocol.md'
    record = dict(protocol_version=VERSION, protocol_sha256=digest(protocol), script_sha256=digest(__file__),
        frozen_gates=GATES, runtime_manifest_sha256=digest(runtime / 'manifest.json'),
        model_sha256=digest(model), platform=platform.platform(), machine=platform.machine(), cpu_count=os.cpu_count(),
        started_at_utc=datetime.now(timezone.utc).isoformat(), sizes_requested=args.sizes,
        status='running', stages=[], scope='synthetic engineering scale; not real knowledge/LLM quality')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    save()
    for size in args.sizes:
        with tempfile.TemporaryDirectory(prefix=f'pka-scale-{size}.') as temporary:
            root = Path(temporary)
            output = root / 'stage.json'
            environment = dict(PATH='/usr/bin:/bin:/usr/sbin:/sbin', HOME=str(root), TMPDIR=str(root),
                PKA_ENVIRONMENT='local', PKA_DATA_DIR=str(root / 'data'), PKA_CACHE_DIR=str(root / 'cache'),
                PKA_LOG_DIR=str(root / 'logs'), PKA_AI_GATEWAY_URL='', PKA_AI_GATEWAY_TOKEN='',
                PKA_SEMANTIC_ENABLED='1', PKA_SEMANTIC_MODEL_PATH=str(runtime / 'model'),
                HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
            try:
                subprocess.run([str(runtime / 'python/bin/python3'), '-I', '-B', str(Path(__file__).resolve()),
                    '--runtime', str(runtime), '--scenario', str(size), '--stage-output', str(output)],
                    env=environment, check=True, timeout=2400)
                record['stages'].append(json.loads(output.read_text()))
            except subprocess.TimeoutExpired:
                record['stages'].append(dict(size=size, status='timeout', timeout_seconds=2400))
            except subprocess.CalledProcessError as error:
                record['stages'].append(dict(size=size, status='execution_failed', exit_code=error.returncode))
            save()
    record['status'] = 'passed' if all(stage['status'] == 'passed' for stage in record['stages']) else 'issues_found'
    record['completed_at_utc'] = datetime.now(timezone.utc).isoformat()
    save()
    print('FINAL: ' + record['status'] + ' ' + str(args.output), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
