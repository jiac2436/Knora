#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ $# -eq 1 ]] || { echo "用法：bash scripts/build_ui_test.sh <已验证的本地运行包路径>" >&2; exit 1; }
RUNTIME="$(cd "$1" && pwd)"
[[ -x "$RUNTIME/python/bin/python3" ]] || { echo "运行包缺少 Python" >&2; exit 1; }
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/pka-ui-test.XXXXXXXX")"
APP="$TEST_ROOT/知识收集箱界面测试.app"
mkdir -p "$APP/Contents/MacOS" .build/module-cache
xcrun swiftc -D CAPTURE_QUEUE_TESTS -parse-as-library -O \
  -target "$(uname -m)-apple-macosx14.0" -module-cache-path "$PWD/.build/module-cache" \
  Sources/*.swift scripts/LocalUITestApp.swift -o "$APP/Contents/MacOS/LocalUITest"
python3 - "$APP" "$TEST_ROOT" "$RUNTIME" <<'PY'
import pathlib
import plistlib
import sys

app, root, runtime = sys.argv[1:]
info = {
    "CFBundleIdentifier": "local.pka.ui-test." + pathlib.Path(root).name.split(".", 1)[1],
    "CFBundleName": "知识收集箱界面测试",
    "CFBundleExecutable": "LocalUITest",
    "CFBundlePackageType": "APPL",
    "CFBundleVersion": "1",
    "LSMinimumSystemVersion": "14.5",
    "NSHighResolutionCapable": True,
    "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    "PKATestRoot": root,
    "PKATestRuntime": runtime,
}
with open(pathlib.Path(app) / "Contents/Info.plist", "wb") as output:
    plistlib.dump(info, output)
print(app)
PY
mkdir -p "$APP/Contents/Resources"
ditto "$PWD/Resources/Capture" "$APP/Contents/Resources/Capture"
codesign --force --sign - "$APP"
