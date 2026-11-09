"""Exercise bundled API/MCP article handoff using an isolated synthetic library."""
import argparse
import asyncio
import json
from pathlib import Path
import select
import subprocess
import tempfile
from uuid import uuid4

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def verify(runtime: Path, root: Path):
    environment = {'PATH': '/usr/bin:/bin', 'HOME': str(root),
        'PKA_ENVIRONMENT': 'local', 'PKA_DATA_DIR': str(root / 'data'),
        'PKA_CACHE_DIR': str(root / 'cache'), 'PKA_LOG_DIR': str(root / 'logs'),
        'PKA_SEMANTIC_ENABLED': '0', 'PKA_WORKER_ROOT': str(runtime / 'backend')}
    credential = 'synthetic-article-handoff-' + uuid4().hex
    with (root / 'host.log').open('wb') as log:
        host = subprocess.Popen([str(runtime / 'python/bin/python3'), '-I', '-B',
            str(runtime / 'backend/app/local_host.py')], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=log, env=environment)
        try:
            host.stdin.write((credential + '\n').encode()); host.stdin.flush()
            assert select.select([host.stdout], [], [], 30)[0], 'Local host did not become ready'
            ready = json.loads(host.stdout.readline())
            assert ready['event'] == 'ready', 'Local host failed to start'
            address = ready['address']
            with httpx.Client(base_url=address, trust_env=False, follow_redirects=False, timeout=15) as client:
                def post(path, payload):
                    result = client.post(path, json=payload, headers={
                        'Authorization': 'Bearer ' + credential, 'Idempotency-Key': str(uuid4())})
                    assert result.status_code == 201, f'Synthetic setup failed at {path}'
                    return result.json()
                domain = post('/v1/domains', {'name': '合成链接验收'})
                source = post('/v1/captures', {'capture_type': 'text', 'title': '合成文章',
                    'content': {'text': '这是临时合成知识库中的文章，仅用于验证本地阅读链接。'},
                    'coverage': 'full_text', 'context': {'domain_id': domain['id']},
                    'client': {'kind': 'desktop', 'version': 'test'}})
                agent = post('/v1/agent-connections', {'client_name': '合成 MCP', 'domain_ids': [domain['id']]})
                parameters = StdioServerParameters(command=str(runtime / 'python/bin/python3'),
                    args=['-I', '-B', str(runtime / 'backend/app/local_mcp.py')],
                    env={**environment, 'PKA_API_FILE': str(root / 'data/agent-api.json'),
                         'PKA_AGENT_TOKEN': agent['access_token']})
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        async def call(tool, payload):
                            result = await session.call_tool(tool, payload)
                            assert not result.isError, f'MCP {tool} failed'
                            return json.loads(result.content[0].text)
                        listing = await call('list_knowledge', {})
                        item = next(item for item in listing['items'] if item['source_id'] == source['id'])
                        expected = f"{address}/open/library/{ready['library_id']}/source/{source['id']}"
                        assert item['citation_url'] == expected
                        results = await call('search_knowledge', {'query': '文章'})
                        assert results['items'][0]['citation_url'] == expected
                        evidence = await call('get_evidence', {'evidence_id': item['evidence_ids'][0]})
                        assert evidence['citation_url'] == expected
                        for tool in ('get_source', 'get_summary', 'get_tags'):
                            assert (await call(tool, {'source_id': source['id']}))['citation_url'] == expected
                        response = client.get(expected)
                        assert response.status_code == 200
                        assert f"href=\"{item['app_url']}\"" in response.text
                        assert '临时合成知识库' not in response.text
                        assert 'no-store' in response.headers['Cache-Control']
                        assert client.get(expected, headers={'Host': 'foreign.example'}).status_code == 404
                        assert client.get(expected + '?redirect=https://example.test').status_code == 404
                        assert client.get('/v1/sources/' + source['id']).status_code == 401
            print('PASS: bundled API, six MCP read paths, HTTP handoff, host checks and app authentication')
        finally:
            host.stdin.close()
            try:
                host.wait(timeout=15)
            except subprocess.TimeoutExpired:
                host.kill(); host.wait(timeout=5)
            host.stdout.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    arguments = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='pka-http-article-test-') as directory:
        asyncio.run(verify(arguments.runtime.resolve(), Path(directory)))
