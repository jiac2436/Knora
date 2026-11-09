"""Verify generated private MCP JSON against synthetic local knowledge using the official SDK."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def payload(result):
    assert not result.isError, "授权工具调用失败"
    return json.loads(result.content[0].text)


async def verify(options):
    servers = json.loads(options.config.read_text())["mcpServers"]
    assert len(servers) == 1, "验收配置只能包含一个合成服务"
    server = next(iter(servers.values()))
    assert set(server) <= {"type", "command", "args", "env"}
    assert server.get("type", "stdio") == "stdio"
    assert Path(server["command"]).is_absolute() and Path(server["command"]).is_file()
    assert len(server["args"]) == 3 and server["args"][:2] == ["-I", "-B"]
    assert Path(server["args"][2]).name == "local_mcp.py" and Path(server["args"][2]).is_file()
    assert set(server["env"]) == {"PKA_API_FILE", "PKA_AGENT_TOKEN"}
    parameters = StdioServerParameters(command=server["command"], args=server["args"], env=server["env"])
    with open(os.devnull, "w") as diagnostics:
        async with stdio_client(parameters, errlog=diagnostics) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "search_knowledge", "get_source", "get_evidence", "get_summary", "get_tags", "list_knowledge",
                }
                result = await session.call_tool("search_knowledge", {"query": options.query, "limit": 20})
                if options.expect_revoked:
                    assert result.isError, "撤销后仍可读取知识"
                    print("PASS: JSON stdio 工具发现及撤销后拒绝读取")
                    return
                items = payload(result)["items"]
                ids = {item["source_id"] for item in items}
                assert options.allowed_source in ids and options.forbidden_source not in ids, "领域隔离失败"
                assert payload(await session.call_tool("get_source", {"source_id": options.allowed_source}))["source_id"] == options.allowed_source
                for tool in ("get_summary", "get_tags"):
                    payload(await session.call_tool(tool, {"source_id": options.allowed_source}))
                item = next(item for item in items if item["source_id"] == options.allowed_source)
                evidence = payload(await session.call_tool("get_evidence", {"evidence_id": item["evidence_id"]}))
                assert evidence["source_id"] == options.allowed_source
                denied = await session.call_tool("get_source", {"source_id": options.forbidden_source})
                assert denied.isError, "未授权原文仍可读取"
                print("PASS: JSON stdio 六工具读取及授权领域隔离；无模型调用")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--allowed-source", required=True)
    parser.add_argument("--forbidden-source", required=True)
    parser.add_argument("--expect-revoked", action="store_true")
    options = parser.parse_args()
    try:
        asyncio.run(asyncio.wait_for(verify(options), timeout=40))
    except Exception:
        parser.exit(1, "FAIL: 合成 JSON MCP 验收失败；未输出配置、凭证或资料。\n")


if __name__ == "__main__":
    main()
