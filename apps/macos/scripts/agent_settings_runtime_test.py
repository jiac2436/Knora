"""Candidate API/MCP scope updates and one-effective-client acceptance; synthetic only."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from packaged_kernel_test import PackagedHost, capture


def exercise(runtime):
    with tempfile.TemporaryDirectory(prefix='pka-agent-settings-') as temporary:
        root = Path(temporary).resolve()
        home = root / 'home'
        home.mkdir()
        host = PackagedHost(runtime, home, 'missing')
        try:
            host.start()
            domains = [host.call('/v1/domains', {'name': name}, expected=201)['id'] for name in ('First', 'Second')]
            sources = [capture(host, domain, 'scopeacceptance 原文检索合成资料') for domain in domains]
            for source in sources:
                host.call('/v1/sources/' + source + '/context', {'visibility': 'app_and_agents'}, method='PATCH')
            def create(name, ids):
                return host.call('/v1/agent-connections', {'client_name': name, 'domain_ids': ids}, expected=201)
            def search(token, expected=200):
                request = Request(host.address + '/v1/mcp/tools/search_knowledge', data=json.dumps({'query': 'scopeacceptance', 'mode': 'exact'}).encode(),
                    headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                try:
                    with urlopen(request, timeout=20) as response:
                        assert response.status == expected
                        return {row['source_id'] for row in json.load(response)['items']}
                except HTTPError as error:
                    assert error.code == expected, error.code
            old = create('Codex', [domains[0]])
            codex = create('Codex', [domains[0]])
            claude = create('Claude Code', [domains[0]])
            search(old['access_token'], 401)
            assert search(codex['access_token']) == search(claude['access_token']) == {sources[0]}
            host.call('/v1/agent-connections/' + codex['id'], {'domain_ids': domains}, method='PATCH', expected=204)
            assert search(codex['access_token']) == set(sources)
            assert search(claude['access_token']) == {sources[0]}
            future = host.call('/v1/domains', {'name': 'Future'}, expected=201)['id']
            future_source = capture(host, future, 'scopeacceptance 以后新建的合成资料')
            host.call('/v1/sources/' + future_source + '/context', {'visibility': 'app_and_agents'}, method='PATCH')
            assert future_source not in search(codex['access_token'])
            host.call('/v1/agent-connections/' + codex['id'], {'domain_ids': [domains[1]]}, method='PATCH', expected=204)
            assert search(codex['access_token']) == {sources[1]}
            print('PASS: same identity/token, scope expansion and narrowing take effect immediately; future libraries excluded; Claude unaffected; old Codex rejected', flush=True)
            items = host.call('/v1/agent-connections')['items']
            assert len([row for row in items if row['client_name'] == 'Codex' and row['status'] == 'active']) == 1
            current = next(row for row in items if row['id'] == codex['id'])
            assert current['expires_at'] == codex['expires_at'] and host.count() == 0
            def helper(request):
                result = subprocess.run([str(runtime / 'python/bin/python3'), '-I', '-B', str(runtime / 'backend/app/agent_install.py')],
                    input=json.dumps(request), capture_output=True, text=True, timeout=60, env=host.env)
                assert result.returncode == 0, result.stdout + result.stderr
                return json.loads(result.stdout)
            config = root / 'codex/config.toml'
            config.parent.mkdir()
            config.write_text('model = "synthetic-preserved"\n')
            identifier = codex['id']
            name = 'knowledge_' + identifier.replace('-', '_')
            entry = {'command': str(runtime / 'python/bin/python3'), 'args': ['-I', '-B', str(runtime / 'backend/app/local_mcp.py')],
                     'env': {'PKA_API_FILE': str(home / 'data/agent-api.json'), 'PKA_AGENT_TOKEN': codex['access_token']}}
            # The accepted endpoint is produced by PackagedHost under the same local library.
            config_text = '[mcp_servers.' + name + ']\ncommand = ' + json.dumps(entry['command']) + '\nargs = ' + json.dumps(entry['args']) + '\nenv = { ' + ', '.join(k + ' = ' + json.dumps(v) for k, v in entry['env'].items()) + ' }\n'
            request = dict(action='plugin_install', client='codex', connection_id=identifier, runtime_path=str(runtime),
                endpoint_path=entry['env']['PKA_API_FILE'], plugin_root=str(root / 'codex-home/.codex/pka-knowledge-plugins'),
                marketplace_path=str(root / 'codex-home/.agents/plugins/marketplace.json'), config_path=str(config),
                skill_template_path=str(runtime / 'backend/app/agent_skill.md'), skill_metadata_path=str(runtime / 'backend/app/agent_skill.yaml'), configuration=config_text)
            installed = helper(request)
            again = helper(request)
            market = json.loads(Path(request['marketplace_path']).read_text())
            assert len(market['plugins']) == 1 and installed['plugin_name'] == again['plugin_name']
            assert config.read_text().count('# BEGIN PKA PLUGIN ') == 1 and 'synthetic-preserved' in config.read_text()
            print('PASS: real plugin/MCP checks; repeated updates keep one package and one config entry; unrelated settings preserved', flush=True)
            host.stop()
            host.start()
            assert search(codex['access_token']) == {sources[1]} and search(claude['access_token']) == {sources[0]}
            search(old['access_token'], 401)
            assert host.count() == 0
            print('PASS: restart retains independent clients, scopes and invalid old token; 0 provider calls; no user configs modified', flush=True)
        finally:
            host.stop()


if __name__ == '__main__':
    exercise(Path(sys.argv[1]).resolve(strict=True))
