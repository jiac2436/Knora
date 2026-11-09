from __future__ import annotations

import json
import os
import sys
from datetime import timedelta
from uuid import uuid4

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy.orm import Session

from app.auth import token_hash
from app.ai import context_state
from app.database import Base, make_engine
from app.models import (
    AgentConnection,
    AIArtifact,
    ConnectionGrant,
    Domain,
    EvidenceChunk,
    PersonalContext,
    Source,
    SourceRevision,
    User,
    utcnow,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_stdio_client_lists_and_calls_real_mcp_tools(tmp_path):
    database_path = tmp_path / "mcp-e2e.db"
    database_url = f"sqlite:///{database_path}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    agent_token = "pka_e2e_agent_token"

    with Session(engine) as db:
        owner = User(email="mcp-e2e@example.com", name="MCP E2E")
        db.add(owner)
        db.flush()
        domain = Domain(owner_id=owner.id, name="工作")
        db.add(domain)
        db.flush()
        source = Source(
            owner_id=owner.id,
            domain_id=domain.id,
            kind="text",
            title="MCP 真实协议证据",
            raw_input="虚拟列表减少 DOM 节点。",
            visibility="app_and_agents",
            coverage="full_text",
            upload_status="cloud_saved",
            processing_status="usable",
        )
        db.add(source)
        db.flush()
        revision = SourceRevision(source_id=source.id, version=1, extracted_text=source.raw_input)
        db.add(revision)
        db.flush()
        source.current_revision_id = revision.id
        evidence = EvidenceChunk(
                revision_id=revision.id,
                ordinal=0,
                text=source.raw_input,
                locator_start=0,
                locator_end=len(source.raw_input),
            )
        db.add(evidence)
        db.add(PersonalContext(source_id=source.id, notes="商品管理页使用"))
        db.flush()
        snapshot = {"request_id": str(uuid4()), "contract_version": 2, "phase": "single", "kind": "analyze",
                    "source_id": source.id, "revision_id": revision.id, "context": context_state(db, source),
                    "evidence": [{"id": evidence.id, "text": evidence.text, "locator_kind": evidence.locator_kind,
                                  "locator_start": evidence.locator_start, "locator_end": evidence.locator_end}]}
        summary_result = {"contract_version": 2, "summary": "虚拟列表减少 DOM 节点。",
                          "summary_citations": [evidence.id],
                          "overview": {"text": "列表优化", "citations": [evidence.id]}}
        db.add(AIArtifact(owner_id=owner.id, source_id=source.id, revision_id=revision.id,
                          job_id=str(uuid4()), snapshot_json=json.dumps(snapshot), result_json=json.dumps(summary_result),
                          provider="synthetic", model="synthetic", prompt_version="test", usage_json="{}"))
        connection = AgentConnection(
            owner_id=owner.id,
            client_name="official-sdk-e2e",
            token_hash=token_hash(agent_token),
            expires_at=utcnow() + timedelta(days=1),
        )
        db.add(connection)
        db.flush()
        db.add(ConnectionGrant(connection_id=connection.id, domain_id=domain.id))
        db.commit()

    environment = os.environ.copy()
    environment["PKA_DATABASE_URL"] = database_url
    environment["PKA_ENVIRONMENT"] = "test"
    environment["PKA_AGENT_TOKEN"] = agent_token
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server"],
        env=environment,
    )
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            initialized = await session.initialize()
            assert '面向用户的引用只写资料标题' in initialized.instructions
            assert '不在答案或来源列表中展示完整或缩写 UUID' in initialized.instructions
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "search_knowledge",
                "get_evidence",
                "get_source",
                "get_summary",
                "get_tags",
                "list_knowledge",
            }
            schema = next(tool.inputSchema for tool in tools.tools if tool.name == "search_knowledge")
            assert schema["properties"]["limit"]["maximum"] == 20
            assert schema["properties"]["limit"]["minimum"] == 1
            assert schema["properties"]["query"]["minLength"] == 1
            result = await session.call_tool("search_knowledge", {"query": "虚拟列表"})
            assert result.isError is False
            payload = json.loads(result.content[0].text)
            assert payload["items"][0]["title"] == "MCP 真实协议证据"
            source_id = payload["items"][0]["source_id"]
            summary = await session.call_tool("get_summary", {"source_id": source_id})
            assert not summary.isError
            summary_payload = json.loads(summary.content[0].text)
            assert summary_payload["available"] is True
            tags = await session.call_tool("get_tags", {"source_id": source_id})
            assert not tags.isError
            assert json.loads(tags.content[0].text)["items"] == []
            evidence = await session.call_tool("get_evidence", {"evidence_id": summary_payload["summary_citations"][0]["evidence_id"]})
            assert not evidence.isError
