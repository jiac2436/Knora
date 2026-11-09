"""Real packaged plugin consolidation and relocation; isolated user/config/cache only."""
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from packaged_kernel_test import PackagedHost, capture


def exercise(runtime):
    with tempfile.TemporaryDirectory(prefix='pka-plugin-singleton-') as temporary:
        root = Path(temporary).resolve()
        home = root / 'home'
        home.mkdir()
        host = PackagedHost(runtime, home, 'missing')
        try:
            host.start()
            domain = host.call('/v1/domains', {'name': '合成插件验收'}, expected=201)['id']
            capture(host, domain, '合成插件连接检查资料，不发送模型。')
            config = root / 'fake-home/.codex/config.toml'
            config.parent.mkdir(parents=True)
            config.write_text('model = "synthetic-preserved"\n')
            def connection():
                return host.call('/v1/agent-connections', {'client_name': 'Codex', 'domain_ids': [domain]}, expected=201)
            def request(grant):
                entry = dict(command=str(runtime / 'python/bin/python3'), args=['-I', '-B', str(runtime / 'backend/app/local_mcp.py')],
                    env=dict(PKA_API_FILE=str(home / 'data/agent-api.json'), PKA_AGENT_TOKEN=grant['access_token']))
                name = 'knowledge_' + grant['id'].replace('-', '_')
                configuration = '[mcp_servers.' + name + ']\ncommand = ' + json.dumps(entry['command']) + '\nargs = ' + json.dumps(entry['args']) + '\nenv = { ' + ', '.join(k + ' = ' + json.dumps(v) for k, v in entry['env'].items()) + ' }\n'
                return dict(action='plugin_install', client='codex', connection_id=grant['id'], runtime_path=str(runtime),
                    endpoint_path=entry['env']['PKA_API_FILE'], plugin_root=str(config.parent / 'pka-knowledge-plugins'),
                    marketplace_path=str(root / 'fake-home/.agents/plugins/marketplace.json'), config_path=str(config),
                    skill_template_path=str(runtime / 'backend/app/agent_skill.md'), skill_metadata_path=str(runtime / 'backend/app/agent_skill.yaml'), configuration=configuration)
            def helper(value, expected=0):
                process = subprocess.run([str(runtime / 'python/bin/python3'), '-I', '-B', str(runtime / 'backend/app/agent_install.py')],
                    input=json.dumps(value), capture_output=True, text=True, env=host.env, timeout=60)
                assert process.returncode == expected, process.stdout + process.stderr
                return json.loads(process.stdout)
            first = connection()
            old = helper(request(first))
            current = connection()
            command = request(current)
            installed = helper(command)
            market = Path(command['marketplace_path'])
            assert [row['name'] for row in json.loads(market.read_text())['plugins']] == [installed['plugin_name']]
            assert installed['retired_plugins'] == [old['plugin_name']]
            assert Path(old['plugin_path']).is_dir()
            assert 'synthetic-preserved' in config.read_text()
            print('PASS: new real grant/plugin retires old listing; one entry remains; old package and unrelated settings preserved', flush=True)
            cache = config.parent / 'plugins/cache' / installed['marketplace_name'] / installed['plugin_name'] / 'local'
            cache.parent.mkdir(parents=True)
            shutil.copytree(installed['plugin_path'], cache)
            check = {key: value for key, value in command.items() if key != 'configuration'}
            assert helper(dict(check, action='plugin_check'))['checked']
            moved = root / 'moved-runtime'
            subprocess.run(['cp', '-cR', str(runtime), str(moved)], check=True)
            check['runtime_path'] = str(moved)
            outdated = helper(dict(check, action='plugin_status'))
            assert outdated['update_required'] and outdated['plugin_status'] == 'needs_update'
            assert helper(dict(check, action='plugin_check'), expected=1)
            repaired = helper(dict(check, action='plugin_install'))
            assert repaired['plugin_name'] == installed['plugin_name'] and not repaired['host_installed']
            assert len(json.loads(market.read_text())['plugins']) == 1
            private = config.parent / 'pka-knowledge-plugins-credentials' / (current['id'] + '.json')
            saved = json.loads(private.read_text())
            assert saved['env']['PKA_AGENT_TOKEN'] == current['access_token']
            assert saved['command'] == str(moved / 'python/bin/python3')
            print('PASS: moved App is distinguished from modified files; repair reuses verified token without keychain/input credentials; plugin identity stays stable', flush=True)
            # Simulate the supported host refresh only inside this disposable fake home.
            shutil.rmtree(cache)
            shutil.copytree(installed['plugin_path'], cache)
            assert helper(dict(check, action='plugin_check'))['checked']
            host.stop()
            host.start()
            assert helper(dict(check, action='plugin_check'))['checked']
            assert host.count() == 0
            skill = Path(installed['plugin_path']) / 'skills/knowledge/SKILL.md'
            skill.write_text(skill.read_text() + '\nUser modified content.\n')
            assert helper(dict(check, action='plugin_status'))['plugin_status'] == 'modified'
            helper(dict(check, action='plugin_install'), expected=1)
            assert skill.read_text().endswith('User modified content.\n')
            print('PASS: refreshed fixture cache and restart pass real MCP; truly modified files remain untouched; 0 model calls and no real host-cache writes', flush=True)
        finally:
            host.stop()


if __name__ == '__main__':
    exercise(Path(sys.argv[1]).resolve(strict=True))
