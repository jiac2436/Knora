"""Isolated synthetic-corpus acceptance, optionally through the real paid gateway.

Credentials remain in process memory. No user library is opened or changed.
"""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "services/api"), str(REPO)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--real", action="store_true", help="Explicitly authorize real model requests")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8091")
    parser.add_argument("--keychain-service")
    parser.add_argument("--sample-id", action="append", help="Only analyze these sample IDs; retrieval still checks the full corpus")
    parser.add_argument("--rag-only", action="store_true", help="Run six real original-evidence questions instead of analysis")
    args = parser.parse_args()
    if args.rag_only and not args.real:
        raise SystemExit("真实问答验收需同时显式指定 --real")
    token = os.getenv("PKA_AI_GATEWAY_TOKEN", "")
    if args.real and not token and args.keychain_service:
        value = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", args.keychain_service,
                                "-a", "session", "-w"], capture_output=True, text=True, timeout=30)
        if value.returncode == 0:
            token = value.stdout.strip()
    if args.real and not token:
        raise SystemExit("真实验收缺少可用的网关访问凭证；没有发送模型请求")

    import httpx
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from app import ai, semantic, summary_index, services
    from app.auth import AppPrincipal
    from app.database import Base, make_engine
    from app.models import User, Domain, AIJob, AIJobStep, AIArtifact, KnowledgeTag
    from app.schemas import CaptureCreate, ConnectionCreate, SearchRequest
    from app.ai_pipeline import pipeline_view
    from services.worker import ai_analysis as worker

    configured = replace(ai.settings, ai_gateway_url=args.gateway_url, ai_gateway_token=token)
    ai.settings = worker.settings = configured
    semantic.settings = replace(semantic.settings, semantic_enabled=True,
                                 semantic_model_path=str(args.runtime / "model"))
    if args.real:
        # Invalid kind in v3 must fail before the provider. This checks the running version without billing.
        with httpx.Client(trust_env=False, timeout=10) as client:
            probe = client.post(args.gateway_url.rstrip("/") + "/v1/analyze",
                headers={"Authorization": "Bearer " + token}, json={"contract_version": 3,
                "request_id": str(uuid4()), "kind": "organize", "source_id": "probe", "revision_id": "probe",
                "evidence": [{"id": "probe", "text": "版本检查", "locator_kind": "text", "locator_start": 0, "locator_end": 4}]})
            if probe.status_code != 422:
                raise SystemExit(f"网关版本/凭证检查未通过，HTTP {probe.status_code}；没有发送有效模型请求")

    samples = json.loads((REPO / "scripts/tests/fixtures/ai-product-samples.json").read_text())
    if args.sample_id and not set(args.sample_id).issubset({sample['id'] for sample in samples}):
        raise SystemExit("未知样本编号；未发送有效模型请求")
    report = {"date": datetime.now(timezone.utc).isoformat(), "real_model": args.real,
              "sample_count": len(samples), "selected_sample_ids": args.sample_id,
              "model": None, "baseline": [], "analysis": [], "retrieval": []}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    def save():
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    with tempfile.TemporaryDirectory(prefix="pka-ai-quality.") as folder:
        engine = make_engine("sqlite:///" + str(Path(folder) / "knowledge.db"))
        Base.metadata.create_all(engine)
        with Session(engine, expire_on_commit=False) as db:
            owner = User(email="quality@example.test", name="Synthetic acceptance")
            db.add(owner); db.flush()
            domain = Domain(owner_id=owner.id, name="合成验收资料")
            db.add(domain); db.commit()
            principal = AppPrincipal(owner.id)
            for name in ["技术文档", "会议记录", "操作指南"]:
                db.add(KnowledgeTag(owner_id=owner.id, dimension="type", name=name, normalized_name=name))
            db.commit()
            sources = {}
            for sample in samples:
                source, _ = services.create_capture(db, owner.id, str(uuid4()), CaptureCreate.model_validate({
                    "capture_type": "browser_selection", "title": sample["title"], "coverage": sample["coverage"],
                    "content": {"text": sample["text"], "image_urls": []},
                    "context": {"domain_id": domain.id}, "client": {"kind": "desktop", "version": "quality-test"}}))
                db.commit(); sources[sample["id"]] = source.id
            connection, _ = services.create_connection(db, owner.id, ConnectionCreate(
                client_name="合成质量验收", domain_ids=[domain.id], expires_in_days=1))
            db.commit()
            while semantic.index_one(db):
                pass

            def retrieval():
                results = []
                for sample in samples:
                    for question in sample["expected_questions"]:
                        def search(query):
                            hits = services.search_knowledge(db, owner.id, SearchRequest(query=query, limit=20),
                                                             allowed_domains={domain.id}, for_agent=True)
                            correct = []
                            for hit in hits:
                                if hit["source_id"] == sources[sample["id"]]:
                                    evidence = services.agent_evidence(db, owner.id, connection.id, hit["evidence_id"], context_window=400)
                                    if question["expected_fragment"] in evidence["text"]:
                                        correct.append(hit)
                            return hits, correct
                        hits, correct = search(question["query"])
                        row = {"sample_id": sample["id"], **question, "first_pass": bool(correct),
                               "source_in_top20": any(h["source_id"] == sources[sample["id"]] for h in hits),
                               "hits": [{k: h.get(k) for k in ["source_id", "evidence_id", "snippet", "match_sources"]} for h in hits]}
                        if not correct:
                            _, fallback = search(question["expected_fragment"])
                            row["exact_fragment_fallback"] = bool(fallback)
                        results.append(row)
                db.rollback()
                return results

            report["baseline"] = retrieval(); save()
            print("BASELINE", sum(r["first_pass"] for r in report["baseline"]), "/", len(report["baseline"]), flush=True)
            if args.real and not args.rag_only:
                for sample in samples:
                    if args.sample_id and sample['id'] not in args.sample_id:
                        continue
                    source_id = sources[sample["id"]]
                    plan = ai.preview_plan(ai.PlanRequest(source_id=source_id, kind="analyze", contract_version=3), principal, db)
                    created = ai.create_job(ai.JobRequest(source_id=source_id, kind="analyze", contract_version=3,
                        request_key=uuid4(), plan_hash=plan["plan_hash"]), principal, db)
                    started = time.monotonic()
                    diagnostics = []
                    class DiagnosticTransport(httpx.BaseTransport):
                        """Capture safe diagnostics from the original call without resending it."""
                        def __init__(self):
                            self.inner = httpx.HTTPTransport(retries=0)
                        def handle_request(self, request):
                            response = self.inner.handle_request(request)
                            if response.status_code == 502:
                                response.read()
                                try:
                                    detail = response.json().get('detail', {})
                                    if detail.get('code') == 'invalid_provider_result':
                                        diagnostics.append({key: detail.get(key) for key in
                                            ['code', 'stage', 'reason', 'fields', 'validation_types', 'json_diagnostic', 'normalization_diagnostic']})
                                except (ValueError, AttributeError):
                                    pass
                            return response
                        def close(self):
                            self.inner.close()
                    while True:
                        with DiagnosticTransport() as transport:
                            if not worker.run_ai_once(db, transport):
                                break
                    while summary_index.index_summary_once(db):
                        pass
                    db.expire_all()
                    job = db.get(AIJob, created["id"])
                    artifact = db.scalar(select(AIArtifact).where(AIArtifact.job_id == job.id))
                    row = {"sample_id": sample["id"], "status": job.status, "error": job.last_error,
                           "source_id": source_id, "job_id": job.id,
                           "calls_planned": plan["calls"], "seconds": round(time.monotonic() - started, 2),
                           "pipeline": pipeline_view(db, job=job)}
                    if diagnostics:
                        row['gateway_diagnostics'] = diagnostics
                    steps = db.scalars(select(AIJobStep).where(AIJobStep.job_id == job.id).order_by(AIJobStep.ordinal)).all()
                    if steps:
                        row['steps'] = [{'phase': step.phase, 'status': step.status, 'error': step.error,
                                         'response': json.loads(step.response_json) if step.response_json else None}
                                        for step in steps]
                    if artifact:
                        row.update(result=json.loads(artifact.result_json), evidence=json.loads(artifact.snapshot_json)["evidence"],
                            usage=json.loads(artifact.usage_json), model=artifact.model, prompt_version=artifact.prompt_version)
                        report["model"] = artifact.model
                    report["analysis"].append(row); save()
                    print(sample["id"], job.status, row["pipeline"]["pipeline_status"], flush=True)
                    if job.status != "completed":
                        # Save only synthetic inputs, never the access credential.
                        row["snapshot"] = json.loads(job.snapshot_json)
                        report["stopped_on_failure"] = sample["id"]; save()
                        raise SystemExit("样本分析失败，已停止后续付费调用；报告保留，不自动重试")
                report["retrieval"] = retrieval(); save()
            else:
                report["retrieval"] = report["baseline"]; save()
            if args.rag_only:
                from app import rag
                from app.models import RAGTurn
                from services.worker import rag_answer
                rag.settings = rag_answer.settings = configured
                session = rag.create_session(rag.SessionCreate(title='合成原文问答验收'), principal, db)
                report['rag'] = []
                for sample_id in ['short-note', 'contradictory-record', 'numeric-fidelity',
                                  'prompt-injection', 'partial-web', 'long-multichapter']:
                    if args.sample_id and sample_id not in args.sample_id:
                        continue
                    sample = next(s for s in samples if s['id'] == sample_id)
                    question = sample['expected_questions'][0]['query']
                    created = rag.create_turn(rag.TurnCreate(session_id=session['id'], question=question,
                        confirm_send=True, domain_ids=[domain.id]), str(uuid4()), principal, db)
                    while rag_answer.run_rag_once(db):
                        pass
                    db.expire_all()
                    turn = db.get(RAGTurn, created['id'])
                    row = {'sample_id': sample_id, **rag.turn_view(db, turn),
                           'result': json.loads(turn.result_json)}
                    row['created_at'] = turn.created_at.isoformat()
                    report['rag'].append(row); save()
                    print('RAG', sample_id, turn.status, flush=True)
                    if turn.status != 'succeeded':
                        raise SystemExit('真实问答验收失败，已停止后续付费调用')
        engine.dispose()
    print("REPORT", args.report, flush=True)


if __name__ == "__main__":
    main()
