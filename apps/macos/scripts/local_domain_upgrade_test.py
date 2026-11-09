"""Real local_host v8 -> current runtime upgrade, only synthetic temporary data.

Use the new packaged runtime's Python with -I -B and pass old/new runtimes.
"""
import argparse
import ast
from pathlib import Path
import sqlite3
import sys
import tempfile

# Explicitly load only the neighboring acceptance helper under isolated Python.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_domain_runtime_test import Host


def inspect_database(path):
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        version = db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('old_runtime', type=Path)
    parser.add_argument('new_runtime', type=Path)
    args = parser.parse_args()
    runtimes = [path.resolve(strict=True) for path in (args.old_runtime, args.new_runtime)]
    for runtime in runtimes:
        assert (runtime / 'python/bin/python3').is_file()
        assert (runtime / 'backend/app/local_host.py').is_file()
    source = ast.parse((runtimes[1] / 'backend/app/migrations.py').read_text())
    expected = next(ast.literal_eval(node.value) for node in source.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'CURRENT_VERSION' for target in node.targets))
    assert expected >= 13, 'current domain runtime required'
    old = new = None
    with tempfile.TemporaryDirectory(prefix='pka-domain-upgrade.') as temporary:
        root = Path(temporary)
        database = root / 'data/knowledge.db'
        try:
            old = Host(runtimes[0], root, '')
            old.env['PKA_AI_GATEWAY_TOKEN'] = ''
            old.start()
            owner = old.call('/v1/me')['id']
            domain = old.call('/v1/domains', {'name': '合成升级验收领域'}, expected=201)['id']
            text = '合成升级验收：已有资料正文、归属和本地身份应在版本升级后完整保留。'
            source = old.call('/v1/captures', dict(capture_type='text', title='合成升级资料',
                coverage='full_text', content={'text': text}, context={'domain_id': domain},
                client={'kind': 'desktop', 'version': 'synthetic-upgrade'}), expected=201)['id']
            assert old.call('/v1/sources/' + source)['extracted_text'] == text
            credential = old.credential
            old.stop()
            assert inspect_database(database) == 8
            prior_backups = set((root / 'data/backups').glob('pre-migration-*.sqlite'))
            print('PASS: 旧v8真实宿主创建资料，数据库完整且schema8', flush=True)
            new = Host(runtimes[1], root, '')
            new.credential = credential
            new.env['PKA_AI_GATEWAY_TOKEN'] = ''
            new.start()
            assert inspect_database(database) == expected
            assert new.call('/v1/me')['id'] == owner
            detail = new.call('/v1/sources/' + source)
            assert detail['domain_id'] == domain and detail['extracted_text'] == text
            assert detail['title'] == '合成升级资料'
            backups = set((root / 'data/backups').glob('pre-migration-*.sqlite')) - prior_backups
            assert len(backups) == 1, 'expected one pre-migration backup'
            assert inspect_database(next(iter(backups))) == 8
            nav = new.call('/v1/domains/' + domain + '/analysis/navigation', {})
            assert nav['node']['id'] == domain
            assert not nav['overview']['available']
            assert source in {item['id'] for item in nav['sources']['items']}
            assert new.call('/v1/ai/config')['configured'] is False
            print(f'PASS: 完整App宿主真实升级至v{expected}，身份/资料保留，schema8迁移备份存在', flush=True)
            new.stop(); new.start()
            assert inspect_database(database) == expected
            assert new.call('/v1/me')['id'] == owner
            assert new.call('/v1/sources/' + source)['extracted_text'] == text
            assert source in {item['id'] for item in new.call(
                '/v1/domains/' + domain + '/analysis/navigation', {})['sources']['items']}
            assert set((root / 'data/backups').glob('pre-migration-*.sqlite')) == prior_backups | backups
            print(f'PASS: v{expected}重启稳定，无再次迁移或重复备份', flush=True)
        except BaseException:
            print('FAIL: 隔离升级验收；宿主日志如下', file=sys.stderr)
            for host in (new, old):
                if host:
                    try:
                        host.stop()
                    except Exception as error:
                        print(str(error), file=sys.stderr)
            log = root / 'host.log'
            if log.exists():
                print(log.read_text(errors='replace')[-12000:], file=sys.stderr)
            raise
        finally:
            for host in (new, old):
                if host:
                    host.stop()
    print(f'PASS: v8→v{expected}真实迁移完成，未使用用户库或AI网关，临时资源已清理', flush=True)


if __name__ == '__main__':
    main()
