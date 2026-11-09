"""M5 portable backup/restore with a packaged CLI and synthetic media only."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent))
from packaged_kernel_test import PackagedHost, capture


def main(runtime):
    with tempfile.TemporaryDirectory(prefix='pka-m5-backup-') as temporary:
        root = Path(temporary)
        first = root / 'first'
        first.mkdir()
        host = PackagedHost(runtime, first, 'missing')
        try:
            host.start()
            domain = host.call('/v1/domains', {'name': '合成备份'}, expected=201)['id']
            source = capture(host, domain, '合成知识加工：备份保留原文，恢复不继承设备执行队列。')
            host.stop()
            package = root / 'synthetic.pka-backup'
            command = [str(runtime / 'python/bin/python3'), '-I', '-B', str(runtime / 'backend/app/local_backup.py')]
            def run(action, location, credential=''):
                args = command + ['--action', action, '--package', str(package), '--data-dir', str(location / 'data'),
                                  '--cache-dir', str(location / 'cache'), '--log-dir', str(location / 'logs')]
                result = subprocess.run(args, input=(credential + '\n').encode(), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, env=host.env, timeout=90)
                assert result.returncode == 0, 'packaged backup CLI failed: ' + result.stdout.decode(errors='replace')[:1000]
                return json.loads(result.stdout)
            # Execution files must stay on the original device, outside backup.
            execution = first / 'data/jobs/capture-queue-v1'
            execution.mkdir(parents=True, exist_ok=True)
            (execution / (str(uuid4()) + '.json')).write_text('{"synthetic_device_only":true}')
            run('export', first)
            run('verify', first)
            with zipfile.ZipFile(package) as archive:
                assert not any('capture-queue' in name for name in archive.namelist())
            restored = root / 'restored'
            restored.mkdir()
            second = PackagedHost(runtime, restored, 'missing')
            run('restore', restored, second.credential)
            assert not (restored / 'data/jobs/capture-queue-v1').exists()
            try:
                second.start()
                assert '恢复不继承' in second.call('/v1/sources/' + source)['extracted_text']
                assert second.call('/v1/capture-queue')['items'] == []
                assert second.count() == 0
            finally:
                second.stop()
            print('PASS: 随包CLI导出、验证、隔离恢复；原文保留，设备采集队列和模型凭证不恢复，无模型调用')
        finally:
            host.stop()


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve(strict=True))
