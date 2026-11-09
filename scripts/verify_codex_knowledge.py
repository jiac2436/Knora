"""Opt-in real Codex acceptance using isolated synthetic knowledge, never user data.

Run with the repository venv. Uses existing Codex login and its model quota.
No persistent MCP configuration is changed. Raw CLI logs stay in the temp directory.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-codex", action="store_true", required=True)
    parser.add_argument("--auto-reference", action="store_true", help="验证 Skill/触发规则自动检索及纯翻译不检索")
    parser.add_argument("--runtime", type=Path, help="改用打包运行环境的隔离 MCP 入口")
    options = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="pka-codex-") as directory:
        work = Path(directory).resolve()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        database_url = f"sqlite:///{work / 'knowledge.db'}"
        environment = {**os.environ, "PYTHONPATH": str(repo / "services/api"),
                       "PKA_DATABASE_URL": database_url, "PKA_ENVIRONMENT": "test",
                       "PKA_SEMANTIC_ENABLED": "0", "PKA_UPLOAD_DIR": str(work / "uploads")}
        # Set before importing application modules; only this synthetic database is opened.
        os.environ.update({key: value for key, value in environment.items() if key.startswith("PKA_")})
        sys.path.insert(0, str(repo / "services/api"))
        from app.database import SessionLocal
        from app.ai import context_state
        from app.models import AIArtifact, Source
        with (work / "api.log").open("w") as log:
            api = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app",
                                    "--host", "127.0.0.1", "--port", str(port)],
                                   cwd=work, env=environment, stdout=log, stderr=log)
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=10) as client:
                    for _ in range(100):
                        try:
                            if client.get("/health").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        if api.poll() is not None:
                            raise RuntimeError("隔离 API 启动失败")
                        time.sleep(0.1)
                    def post(path, body, headers=None):
                        response = client.post(path, json=body, headers=headers)
                        response.raise_for_status()
                        return response.json()
                    account = post("/v1/dev/sessions", {"email": "synthetic@example.invalid", "name": "合成验收"})
                    headers = {"Authorization": f"Bearer {account['access_token']}"}
                    domain = post("/v1/domains", {"name": "验收"}, headers)
                    nonce = uuid4().hex[:12]
                    text = f"本地检索验收：星舟项目的验收编号是 {nonce}。索引重建不调用云端模型。"
                    if options.auto_reference:
                        text = f"我的名字叫泽诺{nonce}，我的职业是园艺工程师。"
                    source = post("/v1/captures", {"capture_type": "browser_selection", "title": "星舟验收记录",
                        "content": {"text": text, "image_urls": []}, "coverage": "full_text",
                        "context": {"domain_id": domain["id"]},
                        "client": {"kind": "browser_extension", "version": "test"}},
                        {**headers, "Idempotency-Key": uuid4().hex})
                    with SessionLocal() as db:
                        row = db.get(Source, source["id"])
                        chunk = source["evidence"][0]
                        evidence_id = chunk["evidence_id"]
                        snapshot = {"request_id": str(uuid4()), "kind": "analyze", "contract_version": 2,
                            "source_id": row.id, "revision_id": row.current_revision_id, "context": context_state(db, row),
                            "evidence": [{"id": evidence_id, "text": text, "locator_kind": "text_offset",
                                          "locator_start": 0, "locator_end": len(text)}]}
                        result = {"contract_version": 2, "summary": text, "summary_citations": [evidence_id],
                                  "overview": {"text": "星舟项目验收说明", "citations": [evidence_id]}}
                        db.add(AIArtifact(owner_id=row.owner_id, source_id=row.id, revision_id=row.current_revision_id,
                            job_id=str(uuid4()), snapshot_json=json.dumps(snapshot), result_json=json.dumps(result),
                            provider="synthetic", model="synthetic", prompt_version="test", usage_json="{}"))
                        db.commit()
                    connection = post("/v1/agent-connections", {"client_name": "isolated-codex-acceptance",
                        "domain_ids": [domain["id"]], "expires_in_days": 1}, headers)
                    cli_env = {**environment, "PKA_API_URL": f"http://127.0.0.1:{port}",
                               "PKA_AGENT_TOKEN": connection["access_token"]}
                    config = {"command": sys.executable, "args": ["-m", "app.mcp_server"],
                              "cwd": str(repo / "services/api"), "required": True,
                              "env_vars": ["PKA_API_URL", "PKA_AGENT_TOKEN", "PYTHONPATH"],
                              "startup_timeout_sec": 30}
                    if options.runtime:
                        runtime = options.runtime.resolve()
                        config.update(command=str(runtime / "python/bin/python3"),
                                      args=["-I", "-B", str(runtime / "backend/app/local_mcp.py")],
                                      cwd=str(runtime))
                    # JSON string literals and TOML inline table, passed as argv, never through a shell.
                    inline = "{ " + ", ".join(f"{key} = {json.dumps(value)}" for key, value in config.items()) + " }"
                    prompt = (repo / "skills/personal-knowledge/SKILL.md").read_text() + (
                        "\n请仅使用 pka MCP 工具完成验收，不使用 shell/文件/其他服务。搜索本地检索验收，"
                        "读取 get_summary 和 get_tags，再用总结引用的 get_evidence 核对原文。"
                        "回答星舟项目验收编号和索引重建是否调用云端模型，写出资料标题和真实证据 ID。")
                    server_name = "pka"
                    if options.auto_reference:
                        from app.agent_install import execute
                        server_name = "knowledge_" + connection["id"].replace("-", "_")
                        isolated_config = work / "config.toml"
                        isolated_config.write_text(f"[mcp_servers]\n{server_name} = {inline}\n")
                        execute({"action": "reference_enable", "client": "codex", "connection_id": connection["id"],
                                 "config_path": str(isolated_config), "skills_root": str(work / ".agents/skills"),
                                 "instructions_path": str(work / "AGENTS.md")})
                        prompt = "我的名字和职业是什么？请简短回答并给出依据。"
                    with (work / "codex.log").open("w") as cli_log:
                        completed = subprocess.run(["codex", "exec", "--ignore-user-config", "--ephemeral",
                            "--skip-git-repo-check", "--sandbox", "read-only", "-C", str(work),
                            "-c", f"mcp_servers.{server_name}={inline}", "-o", str(work / "answer.txt"), "-"],
                            input=prompt, text=True, env=cli_env, stdout=cli_log, stderr=cli_log, timeout=180)
                    if completed.returncode:
                        detail = (work / "codex.log").read_text()[-1500:]
                        for secret in (account["access_token"], connection["access_token"]):
                            detail = detail.replace(secret, "[redacted]")
                        raise RuntimeError("Codex 验收调用失败；未改动现有配置或真实知识\n" + detail)
                    answer = (work / "answer.txt").read_text()
                    events = client.get("/v1/audit-events", headers=headers).json()["items"]
                    called = {event["tool"] for event in events if event["result"] == "success"}
                    required = {"search_knowledge", "get_evidence"} if options.auto_reference else {"search_knowledge", "get_summary", "get_tags", "get_evidence"}
                    if not required <= called or nonce not in answer or evidence_id not in answer:
                        raise RuntimeError(f"Codex 未完成全部工具与引用验收门槛；成功调用={sorted(called)}\n{answer}")
                    if options.auto_reference:
                        if "园艺工程师" not in answer:
                            raise RuntimeError("自动参考未读到职业")
                        previous_count = len(events)
                        with (work / "codex-negative.log").open("w") as cli_log:
                            completed = subprocess.run(["codex", "exec", "--ignore-user-config", "--ephemeral",
                                "--skip-git-repo-check", "--sandbox", "read-only", "-C", str(work),
                                "-c", f"mcp_servers.{server_name}={inline}", "-o", str(work / "negative.txt"), "-"],
                                input="把 Good morning 翻译成中文。", text=True, env=cli_env, stdout=cli_log, stderr=cli_log, timeout=180)
                        current_events = client.get("/v1/audit-events", headers=headers).json()["items"]
                        if completed.returncode or len(current_events) != previous_count:
                            raise RuntimeError("纯翻译负例失败或不必要地读取了知识库")
                        print("PASS: 真实 Codex 自动选用知识库、读取原文并引用；纯翻译未访问 MCP")
                    else:
                        print("PASS: 真实 Codex → 本地 API 转接 → 搜索/总结/标签/原文 → 含真实证据 ID 回答")
                    print(answer)
            finally:
                api.terminate()
                try:
                    api.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    api.kill(); api.wait()


if __name__ == "__main__":
    main()
