"""Export the existing app brand as Chrome/Edge PNG icon sizes (macOS sips)."""
from pathlib import Path
import subprocess
import argparse
import tempfile

root = Path(__file__).resolve().parents[3]
source = root / 'apps/macos/Resources/Brand/AppIcon.png'
output = root / 'apps/extension/static/icons'
output.mkdir(parents=True, exist_ok=True)
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--toolbar-only", action="store_true")
args = parser.parse_args()
for size in (() if args.toolbar_only else (16, 32, 48, 128)):
    subprocess.run(['sips', '--resampleHeightWidth', str(size), str(size), str(source),
                    '--out', str(output / f'icon-{size}.png')], check=True, capture_output=True)
with tempfile.TemporaryDirectory(prefix='know-toolbar-icons-') as directory:
    temporary = Path(directory)
    executable = temporary / 'generate-toolbar'
    subprocess.run(['xcrun', 'swiftc', '-module-cache-path', str(temporary / 'module-cache'),
                    str(Path(__file__).with_name('GenerateToolbarIcons.swift')), '-o', str(executable)], check=True)
    subprocess.run([str(executable), str(source), str(output)], check=True)
for size in (16, 24, 32, 48):
    (output / f'toolbar-{size}.png').unlink(missing_ok=True)
print('Exported existing app brand icons and toolbar glyphs')
