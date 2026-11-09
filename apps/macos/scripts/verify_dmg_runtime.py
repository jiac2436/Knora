"""Copy a trusted DMG's App and smoke-test it with temporary, empty device state."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).parent / 'runtime'))
from verify_runtime import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dmg', type=Path)
    parser.add_argument('--sha256', required=True, help='Digest from the trusted packaging result')
    args = parser.parse_args()
    if not args.dmg.is_file() or digest(args.dmg) != args.sha256:
        parser.error('DMG checksum does not match the trusted packaging record')
    with tempfile.TemporaryDirectory(prefix='knora-dmg-install-') as temporary:
        root = Path(temporary)
        mount = root / 'mount'; mount.mkdir()
        attached = False
        try:
            subprocess.run(['hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint',
                str(mount), str(args.dmg.resolve())], check=True, capture_output=True)
            attached = True
            app = root / 'installed/Knora.app'
            shutil.copytree(mount / 'Knora.app', app, symlinks=True)
        finally:
            if attached:
                subprocess.run(['hdiutil', 'detach', str(mount)], check=True, capture_output=True)
        subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
        runtime = app / 'Contents/Resources/runtime'
        subprocess.run([str(runtime / 'python/bin/python3'), '-I', '-B',
            str(Path(__file__).with_name('lightweight_runtime_test.py').resolve()), str(runtime)], check=True)
        print('PASS: DMG copy-install, signature, empty device startup, capture/search/MCP, mock AI and restart')


if __name__ == '__main__':
    main()
