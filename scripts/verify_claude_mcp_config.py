#!/usr/bin/env python3
"""Health-check a synthetic stdio configuration with an isolated Claude Code CLI.

Only use trusted test fixtures: Claude executes their stdio commands. This checks
configuration loading and MCP initialization, not tool authorization or answers.
No model prompt is sent, and neither configuration nor CLI output is printed.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile


class VerificationError(Exception):
    pass


def private_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False)


def run_check(claude: str, project: Path, environment: dict) -> str:
    process = subprocess.Popen(
        [claude, "--bare", "mcp", "list"],
        cwd=project,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=45)
        if process.returncode:
            raise VerificationError("Claude MCP 健康检查退出失败")
        return re.sub(r"\x1b\[[0-9;]*m", "", output)
    except subprocess.TimeoutExpired:
        raise VerificationError("Claude MCP 健康检查超时") from None
    finally:
        # Kill any stdio child left behind, including on timeout.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()


def verify(config_path: Path, claude: str) -> None:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise VerificationError("无法读取测试配置 JSON") from None
    if not isinstance(config, dict) or set(config) != {"mcpServers"}:
        raise VerificationError("测试配置必须只包含 mcpServers")
    servers = config["mcpServers"]
    if not isinstance(servers, dict) or not servers:
        raise VerificationError("测试配置没有 stdio 服务")
    for name, server in servers.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise VerificationError("测试服务名称不合法")
        if not isinstance(server, dict) or server.get("type", "stdio") != "stdio":
            raise VerificationError("只支持受信任的 stdio 测试配置")
        if not isinstance(server.get("command"), str) or not Path(server["command"]).is_absolute():
            raise VerificationError("测试服务必须使用绝对命令路径")

    with tempfile.TemporaryDirectory(prefix="pka-claude-mcp-") as directory:
        root = Path(directory).resolve()
        project = root / "project"
        config_home = root / "config"
        project.mkdir(mode=0o700)
        config_home.mkdir(mode=0o700)
        # Do not inherit provider keys, OAuth tokens, plugins or account suffixes.
        # HOME remains unchanged; Claude's global resolver uses CONFIG_DIR.
        environment = {
            key: os.environ[key]
            for key in ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "TZ")
            if key in os.environ
        }
        environment.update({
            "CLAUDE_CONFIG_DIR": str(config_home),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "NO_COLOR": "1",
        })
        private_json(config_home / ".claude.json", {
            "mcpServers": {},
            "projects": {str(project): {"hasTrustDialogAccepted": True}},
        })
        private_json(config_home / "settings.json", {
            "enabledMcpjsonServers": list(servers),
        })
        # Fail before executing the fixture if isolation exposes another server.
        baseline = run_check(claude, project, environment)
        if "No MCP servers configured" not in baseline:
            raise VerificationError("隔离配置基线未通过；未执行测试服务")
        private_json(project / ".mcp.json", config)
        output = run_check(claude, project, environment)
        for name in servers:
            lines = [line for line in output.splitlines() if line.startswith(name + ":")]
            if len(lines) != 1 or "✓ Connected" not in lines[0]:
                raise VerificationError("测试 stdio 服务未连接成功")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--claude", default=shutil.which("claude"))
    arguments = parser.parse_args()
    if not arguments.claude:
        print("FAIL：未找到 Claude Code CLI")
        return 1
    try:
        verify(arguments.config, arguments.claude)
    except VerificationError as error:
        print("FAIL：" + str(error))
        return 1
    except (OSError, ValueError, TypeError):
        print("FAIL：CLI 启动或测试配置格式异常")
        return 1
    print("PASS：Claude Code 隔离配置加载及 stdio MCP 健康检查")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
