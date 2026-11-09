"""Opt-in real Codex plugin acceptance against an isolated synthetic library.

Discovery uses temporary project files and an isolated Codex host cache. Full
model acceptance requires a separately authenticated isolated --host-home;
credentials are never copied. No AGENTS.md, standalone Skill installation,
or command-line MCP override is used. Experimental host RPCs are test-only.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx

CODEX_BINARY = "codex"


def inspect_plugin(work: Path, marketplace: str, name: str, server: str, host_home: Path) -> Path:
    """Read actual host metadata using a disposable Codex home, without model calls."""
    host_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    home = str(host_home)
    diagnostic = work / "plugin-host.log"
    diagnostic_file = diagnostic.open("w")
    process = subprocess.Popen([CODEX_BINARY, "app-server"], cwd=work,
        env={**os.environ, "CODEX_HOME": home, "HOME": str(work)},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=diagnostic_file, text=True)
    try:
        pending = b""
        def request(identifier: int, method: str, params: dict) -> dict:
            nonlocal pending
            process.stdin.write(json.dumps({"id": identifier, "method": method, "params": params}) + "\n")
            process.stdin.flush()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if b"\n" not in pending:
                    if not select.select([process.stdout], [], [], 1)[0]:
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    pending += chunk
                    continue
                line, _, pending = pending.partition(b"\n")
                message = json.loads(line)
                if message.get("id") == identifier:
                    if "error" in message:
                        raise RuntimeError(f"Codex 插件元信息接口失败：{message['error']}")
                    return message["result"]
            raise RuntimeError("Codex 插件元信息接口超时")

        request(1, "initialize", {"clientInfo": {"name": "pka-plugin-acceptance", "version": "1"},
                                   "capabilities": {"experimentalApi": True}})
        process.stdin.write(json.dumps({"method": "initialized"}) + "\n")
        process.stdin.flush()
        detail = request(2, "plugin/read", {"marketplacePath": marketplace, "pluginName": name})["plugin"]
        if server not in detail["mcpServers"] or not detail["skills"]:
            raise RuntimeError("宿主未发现插件的 MCP 或 Skill，停止模型验收；"
                               f"MCP 数量={len(detail['mcpServers'])}，Skill 数量={len(detail['skills'])}")
        request(3, "plugin/install", {"marketplacePath": marketplace, "pluginName": name})
        request(4, "config/mcpServer/reload", {})
        inventory = request(5, "mcpServerStatus/list", {"detail": "toolsAndAuthOnly"})
        entries = inventory.get("data", [])
        plugin_entries = [entry for entry in entries if server in entry.get("name", "")]
        tool_names = {name for entry in plugin_entries for name in entry.get("tools", {})}
        if not any("get_evidence" in name for name in tool_names):
            summary = [(entry.get("name"), list(entry.get("tools", {}))) for entry in entries]
            raise RuntimeError(f"真实宿主未加载插件原文读取工具，停止模型验收；工具清单={summary}")
        skills = request(6, "skills/list", {"cwds": [str(work)], "forceReload": True})
        owned = [skill for group in skills.get("data", []) for skill in group.get("skills", [])
                 if name in str(skill.get("path", ""))]
        if len(owned) != 1 or not any(tool.get("value") == server
                for tool in (owned[0].get("dependencies") or {}).get("tools", [])):
            raise RuntimeError("宿主未加载当前 Skill 的 MCP 依赖")
        print("HOST: 主动调用 Skill 的 MCP 依赖已识别")
        print(f"HOST: 已发现插件 Skill 和 {len(tool_names)} 个知识库工具")
        cache_verified = False
        for manifest in (host_home / "plugins/cache").rglob("plugin.json"):
            print("HOST_CACHE:", manifest.relative_to(host_home))
            if manifest.parent.name == ".codex-plugin":
                continue
            source = Path(detail["summary"]["source"]["path"])
            expected = ("plugin.json", ".codex-plugin/plugin.json", "mcp.json", ".mcp.json",
                        "skills/knowledge/SKILL.md", "skills/knowledge/agents/openai.yaml")
            cache_verified = all((manifest.parent / relative).read_bytes() == (source / relative).read_bytes()
                                 for relative in expected)
            if not cache_verified:
                raise RuntimeError("宿主缓存与生成插件的文件内容不一致")
        if not cache_verified:
            raise RuntimeError("宿主没有创建可验证的插件缓存")
        print("HOST_CACHE: 六个插件文件与源文件字节一致")
        return host_home
    finally:
        diagnostic_file.close()
        for line in diagnostic.read_text().splitlines():
            if "invalid plugin MCP" in line or "failed to parse" in line:
                print("HOST_DIAGNOSTIC:", line[:1800])
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run_codex(work: Path, environment: dict, label: str, prompt: str, secrets: list[str]) -> tuple[str, str]:
    output = work / f"{label}.txt"
    log_path = work / f"{label}.log"
    with log_path.open("w") as log:
        completed = subprocess.run([
            CODEX_BINARY, "exec", "--ignore-rules", "--ephemeral",
            "--skip-git-repo-check", "--sandbox", "read-only", "-C", str(work),
            "-c", "project_doc_max_bytes=0",
            "-c", f"projects.{json.dumps(str(work))}.trust_level=\"trusted\"",
            "-o", str(output), "-",
        ], input=prompt, text=True, env=environment, stdout=log, stderr=log, timeout=240)
    detail = log_path.read_text()
    for secret in secrets:
        detail = detail.replace(secret, "[redacted]")
    if completed.returncode:
        raise RuntimeError(f"Codex 插件 {label} 验收失败；未修改用户全局配置\n{detail[-2000:]}")
    return output.read_text(), detail


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-codex", action="store_true", required=True)
    parser.add_argument("--runtime", type=Path, required=True, help="完整打包运行环境")
    parser.add_argument("--discovery-only", action="store_true", help="仅检查真实宿主发现与 MCP 读取，不消耗模型额度")
    parser.add_argument("--host-home", type=Path, help="单独登录的隔离 Codex HOME；将安装合成验收插件，不能使用真实日常配置目录")
    parser.add_argument("--codex-bin", type=Path, help="目标宿主可执行文件；默认优先使用本机桌面内置版本")
    options = parser.parse_args()
    global CODEX_BINARY
    desktop = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")
    selected = options.codex_bin or (desktop if desktop.is_file() else shutil.which("codex"))
    if selected is None:
        parser.error("找不到 Codex 宿主")
    CODEX_BINARY = str(Path(selected).resolve())
    version = subprocess.run([CODEX_BINARY, "--version"], capture_output=True, text=True, check=True)
    print("TARGET_HOST:", CODEX_BINARY, version.stdout.strip())
    runtime = options.runtime.resolve()
    for relative in ("python/bin/python3", "backend/app/local_mcp.py"):
        if not (runtime / relative).is_file():
            parser.error(f"打包运行环境缺少 {relative}")
    help_result = subprocess.run([CODEX_BINARY, "plugin", "--help"], capture_output=True, text=True)
    if help_result.returncode or "marketplace" not in help_result.stdout:
        raise RuntimeError("此 Codex CLI 不支持插件，无法执行真实插件验收")
    repo = Path(__file__).resolve().parents[1]
    if options.host_home:
        actual_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).resolve()
        if options.host_home.resolve() == actual_home:
            parser.error("不能使用真实日常 Codex HOME；请提供单独登录的隔离验收目录")
    with tempfile.TemporaryDirectory(prefix="pka-codex-plugin-") as directory:
        work = Path(directory).resolve()
        os.chmod(work, 0o700)
        # Establish a project root so project-local marketplace and config are discovered.
        subprocess.run(["git", "init", "--quiet", str(work)], check=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        environment = {**os.environ, "PYTHONPATH": str(repo / "services/api"),
                       "PKA_DATABASE_URL": f"sqlite:///{work / 'knowledge.db'}",
                       "PKA_ENVIRONMENT": "test", "PKA_SEMANTIC_ENABLED": "0",
                       "PKA_UPLOAD_DIR": str(work / "uploads")}
        os.environ.update({key: value for key, value in environment.items() if key.startswith("PKA_")})
        sys.path.insert(0, str(repo / "services/api"))
        from app.agent_plugin import execute
        from app.agent_endpoint import published_endpoint

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
                    else:
                        raise RuntimeError("隔离 API 启动超时")

                    def post(path: str, body: dict, headers: dict | None = None) -> dict:
                        response = client.post(path, json=body, headers=headers)
                        response.raise_for_status()
                        return response.json()

                    account = post("/v1/dev/sessions", {"email": "plugin@example.invalid", "name": "合成插件验收"})
                    headers = {"Authorization": f"Bearer {account['access_token']}"}
                    domain = post("/v1/domains", {"name": "合成项目资料"}, headers)
                    nonce = uuid4().hex[:12]
                    source = post("/v1/captures", {
                        "capture_type": "browser_selection", "title": "索引设计说明",
                        "content": {"text": f"项目索引采用分段检索，验证标识为{nonce}，原文证据用于核验结论。", "image_urls": []},
                        "coverage": "full_text", "context": {"domain_id": domain["id"]},
                        "client": {"kind": "browser_extension", "version": "plugin-test"}},
                        {**headers, "Idempotency-Key": uuid4().hex})
                    evidence_id = source["evidence"][0]["evidence_id"]
                    connection = post("/v1/agent-connections", {
                        "client_name": "isolated-codex-plugin", "domain_ids": [domain["id"]],
                        "expires_in_days": 1}, headers)
                    server = "knowledge_" + connection["id"].replace("-", "_")
                    with published_endpoint(work, f"http://127.0.0.1:{port}", uuid4().hex) as endpoint:
                        env_table = {"PKA_API_FILE": str(endpoint), "PKA_AGENT_TOKEN": connection["access_token"]}
                        configuration = (
                            f"[mcp_servers.{server}]\ncommand = {json.dumps(str(runtime / 'python/bin/python3'))}\n"
                            f"args = {json.dumps(['-I', '-B', str(runtime / 'backend/app/local_mcp.py')])}\n"
                            "env = { " + ", ".join(f"{key} = {json.dumps(value)}" for key, value in env_table.items()) + " }\n")
                        host_home = options.host_home.resolve() if options.host_home else work / ".codex"
                        request = {"client": "codex", "connection_id": connection["id"],
                                   "runtime_path": str(runtime), "endpoint_path": str(endpoint),
                                   "plugin_root": str(work / ".agents/plugins/sources"),
                                   "marketplace_path": str(work / ".agents/plugins/marketplace.json"),
                                   "skill_template_path": str(repo / "skills/personal-knowledge/SKILL.md"),
                                   "config_path": str(host_home / "config.toml")}
                        installed = execute({**request, "action": "plugin_install", "configuration": configuration})
                        inspect_plugin(work, request["marketplace_path"], installed["plugin_name"], server, host_home)
                        execute({**request, "action": "plugin_host_check"})
                        if options.discovery_only:
                            print("PASS: 真实 Codex 宿主发现插件 MCP 和 Skill；隔离知识库读取检查通过")
                            return
                        # The plugin is the only route to this synthetic grant.
                        cli_environment = {key: value for key, value in environment.items()
                                           if not key.startswith("PKA_") and key != "PYTHONPATH"}
                        # Use the same isolated host cache. Authentication is checked by
                        # Codex itself; no credential file is read or copied by this script.
                        cli_environment["CODEX_HOME"] = str(host_home)
                        cli_environment["HOME"] = str(work)
                        for key in ("CODEX_APP_TOOLS_PIPE_PATH", "CODEX_SESSION_ID", "CODEX_THREAD_ID",
                                    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_TASK_WORKSPACE_VERIFYING_IDENTITY"):
                            cli_environment.pop(key, None)
                        login = subprocess.run([CODEX_BINARY, "login", "status"], env=cli_environment,
                                               text=True, capture_output=True, timeout=20)
                        if login.returncode:
                            raise RuntimeError("宿主发现和 MCP 读取已通过；隔离 Codex HOME 未登录，"
                                               "已停止模型验收。请单独授权登录隔离目录，再传 --host-home；"
                                               "不会复制真实登录凭证或修改日常 Codex 配置。")
                        secrets = [account["access_token"], connection["access_token"]]
                        previous = client.get("/v1/audit-events", headers=headers).json()["items"]
                        answer, _ = run_codex(work, cli_environment, "knowledge",
                            f"$pka-knowledge-{connection['id']}:knowledge 请查询索引设计说明，说明检索方式和验证标识，并给出原文依据。", secrets)
                        events = client.get("/v1/audit-events", headers=headers).json()["items"]
                        baseline_ids = {event["id"] for event in previous}
                        calls = {event["tool"] for event in events
                                 if event["id"] not in baseline_ids and event["result"] == "success"}
                        if not {"search_knowledge", "get_evidence"} <= calls or not all(
                                value in answer for value in (nonce, "分段检索", evidence_id)):
                            raise RuntimeError(f"真实插件主动调用检索或原文引用未通过；成功调用={sorted(calls)}\n{answer}")
                        previous_ids = {event["id"] for event in events}
                        translation, _ = run_codex(work, cli_environment, "translation",
                                                  "把 Good morning 翻译成中文。", secrets)
                        events = client.get("/v1/audit-events", headers=headers).json()["items"]
                        if {event["id"] for event in events} != previous_ids or not any(
                                value in translation for value in ("早上好", "早安")):
                            raise RuntimeError("插件纯翻译负例失败或发生不必要的知识库访问")
                        execute({**request, "action": "plugin_disable"})
                        disabled, disabled_log = run_codex(work, cli_environment, "disabled",
                            "请仅列出当前可用的 MCP 工具名；没有则回答 NONE。不要执行工具或读取文件。", secrets)
                        events = client.get("/v1/audit-events", headers=headers).json()["items"]
                        if ({event["id"] for event in events} != previous_ids
                                or server in disabled_log or "knowledge_" in disabled
                                or any(tool in disabled for tool in ("search_knowledge", "get_evidence"))):
                            raise RuntimeError("插件停用后仍发现知识库工具或产生访问")
                        print("PASS: 真实 Codex 加载本地插件，主动调用检索并引用原文；翻译不检索；停用后知识库工具不可用")
                        print(answer)
            finally:
                api.terminate()
                try:
                    api.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    api.kill()
                    api.wait()


if __name__ == "__main__":
    main()
