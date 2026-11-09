"""Run API regressions from an ephemeral source export, keeping the checkout clean."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test-tools', type=Path, help='Optional isolated pytest installation directory')
    parser.add_argument('tests', nargs='*', default=['services/api/tests',
        'apps/macos/scripts/runtime/test_runtime_tools.py', 'apps/macos/scripts/test_publish_app.py',
        'apps/macos/scripts/test_build_dmg.py'])
    args = parser.parse_args()
    files = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).decode().split('\0')
    with tempfile.TemporaryDirectory(prefix='knora-source-tests-') as temporary:
        location = Path(temporary)
        snapshot = location / 'source'
        snapshot.mkdir()
        for name in files:
            path = ROOT / name
            if not name or not path.is_file():
                continue
            if path.is_symlink():
                raise ValueError('Public source export cannot contain symbolic links: ' + name)
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        (snapshot / '.venv').symlink_to(Path(sys.prefix), target_is_directory=True)
        environment = {key: value for key, value in os.environ.items() if not key.startswith('PKA_')}
        paths = [str(snapshot / 'services/api'), str(snapshot)]
        if args.test_tools:
            paths.insert(0, str(args.test_tools.resolve(strict=True)))
        home = location / 'home'; home.mkdir()
        environment.update(HOME=str(home), PYTHONPATH=os.pathsep.join(paths),
            PYTHONDONTWRITEBYTECODE='1', PKA_ENVIRONMENT='test')
        result = subprocess.run([sys.executable, '-B', '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
            '--basetemp', str(location / 'pytest'), *args.tests], cwd=snapshot, env=environment)
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
