"""Isolated base-package acceptance. Synthetic data/model replies, no real keys."""
from pathlib import Path
import asyncio
import json
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from packaged_kernel_test import PackagedHost, capture, analyze, wait


async def check_base_mcp(host, domain, source):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    host.call('/v1/sources/' + source + '/context', {'visibility': 'app_and_agents'}, method='PATCH')
    token = host.call('/v1/agent-connections', {'client_name': 'synthetic-lightweight',
        'domain_ids': [domain]}, expected=201)['access_token']
    environment = {**host.env, 'PKA_API_URL': host.address, 'PKA_AGENT_TOKEN': token}
    parameters = StdioServerParameters(command=str(host.runtime / 'python/bin/python3'),
        args=['-I', '-B', '-c', 'import sys;sys.path.insert(0,sys.argv[1]);from app.mcp_server import main;main()',
              str(host.runtime / 'backend')], env=environment)
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool('search_knowledge', {'query': '精确搜索', 'mode': 'exact'})
            assert not result.isError, result
            body = result.structuredContent or json.loads(result.content[0].text)
            assert source in {item['source_id'] for item in body['items']}, body
            evidence = await session.call_tool('get_evidence', {'evidence_id': body['items'][0]['evidence_id']})
            assert not evidence.isError, evidence
            missing = await session.call_tool('search_knowledge', {'query': '精确搜索', 'mode': 'semantic'})
            assert missing.isError, missing
    print('PASS: actual base MCP stdio exact search/evidence work; missing semantic returns explicit error', flush=True)


def exercise(runtime):
    with tempfile.TemporaryDirectory(prefix='pka-lightweight-test-') as temporary:
        root = Path(temporary).resolve()
        missing = root / 'missing'
        missing.mkdir()
        host = PackagedHost(runtime, missing, 'missing')
        try:
            host.start()
            state = host.call('/v1/runtime-extensions')
            assert state['cloud_ai'] == 'bundled'
            assert all(value['status'] == 'not_installed' for value in state['extensions'].values()), state
            domain = host.call('/v1/domains', {'name': '轻量基础库'}, expected=201)['id']
            source = capture(host, domain, '轻量基础验收：原文留在本机，精确搜索正常工作。')
            assert host.call('/v1/search', {'query': '精确搜索', 'mode': 'exact'})['items']
            assert host.call('/v1/sources/' + source)['extracted_text']
            asyncio.run(check_base_mcp(host, domain, source))
            assert host.count() == 0
            host.stop()
            host.start()
            assert host.call('/v1/sources/' + source)['id'] == source
            assert host.count() == 0
            print('PASS: no-extension base opens, captures, exact-searches and restarts without model calls', flush=True)
        finally:
            host.stop()
        direct = root / 'direct'
        direct.mkdir()
        host = PackagedHost(runtime, direct)
        try:
            host.start()
            domain = host.call('/v1/domains', {'name': '云端AI仍内置'}, expected=201)['id']
            source = capture(host, domain, '合成知识加工：事实依据原文，图示增长37%。原文保留于本机，摘要与标签帮助查找，概览用于导航，具体判断需要核对证据。' * 3)
            artifact = analyze(host, source)
            assert host.count() == 1, host.count()
            assert host.call('/v1/search', {'query': '合成知识加工', 'mode': 'exact'})['items']
            before = host.count()
            host.stop()
            host.start()
            saved = host.call('/v1/ai/artifacts/' + artifact['id'])
            assert saved['id'] == artifact['id'] and host.count() == before
            print('PASS: bundled cloud executor, real pipeline/index completion and saved result survive restart without semantic/OCR packages', flush=True)
        finally:
            host.stop()


if __name__ == '__main__':
    exercise(Path(sys.argv[1]).resolve(strict=True))
