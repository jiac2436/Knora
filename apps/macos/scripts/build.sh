#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
APP="${PKA_APP_OUTPUT:-$PWD/.build/candidate/Knora.app}"
[[ "$APP" == /* && "$APP" == *.app && ! -L "$APP" ]] || { echo "App 输出须为绝对 .app 路径且不能是符号链接" >&2; exit 1; }
[[ "$APP" != "$PWD/dist/"* ]] || { echo "dist 仅保留正式 Knora.app；请构建到 .build/candidate，再用 publish_app.py 发布已验收包" >&2; exit 1; }
if [[ -z "${PKA_APP_OUTPUT:-}" && -z "${PKA_RUNTIME_PAYLOAD:-}" ]]; then
  echo "完整候选包必须提供运行包及可信摘要；外置后台开发构建请显式指定 .build/dev 下的 PKA_APP_OUTPUT" >&2
  exit 1
fi
mkdir -p "$APP/Contents/MacOS" "$PWD/.build/module-cache"
xcrun swiftc -parse-as-library -O -target "$(uname -m)-apple-macosx14.5" \
  -module-cache-path "$PWD/.build/module-cache" \
  Sources/*.swift -o "$APP/Contents/MacOS/KnowledgeInbox"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>local.pka.knowledge-inbox</string>
<key>CFBundleName</key><string>Knora</string>
<key>CFBundleDisplayName</key><string>知径 · Knora</string>
<key>CFBundleExecutable</key><string>KnowledgeInbox</string>
<key>CFBundleIconFile</key><string>AppIcon.icns</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.2.0</string>
<key>CFBundleVersion</key><string>2</string>
<key>CFBundleURLTypes</key><array><dict>
<key>CFBundleURLName</key><string>local.pka.knowledge-inbox.articles</string>
<key>CFBundleTypeRole</key><string>Viewer</string>
<key>CFBundleURLSchemes</key><array><string>knowledge-inbox</string></array>
</dict></array>
<key>LSMinimumSystemVersion</key><string>14.5</string>
<key>NSHighResolutionCapable</key><true/>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
if [[ -n "${PKA_RUNTIME_PAYLOAD:-}" ]]; then
  [[ -x "$PKA_RUNTIME_PAYLOAD/python/bin/python3" ]] || { echo "运行包缺少独立 Python" >&2; exit 1; }
  [[ -x "$PKA_RUNTIME_PAYLOAD/platform/macos/KnowledgePlatform" ]] || { echo "M4运行包缺少同版系统辅助程序，不能混装旧后台" >&2; exit 1; }
  [[ -n "${PKA_RUNTIME_MANIFEST_SHA256:-}" ]] || { echo "必须提供可信运行包清单摘要" >&2; exit 1; }
  "$PKA_RUNTIME_PAYLOAD/python/bin/python3" -I -B scripts/runtime/verify_runtime.py \
    "$PKA_RUNTIME_PAYLOAD" --manifest-sha256 "$PKA_RUNTIME_MANIFEST_SHA256"
  mkdir -p "$APP/Contents/Resources"
  rm -rf "$APP/Contents/Resources/runtime"
  ditto "$PKA_RUNTIME_PAYLOAD" "$APP/Contents/Resources/runtime"
else
  # An ordinary development build must not reuse an old bundled runtime.
  rm -rf "$APP/Contents/Resources/runtime"
fi
mkdir -p "$APP/Contents/Resources"
ditto "$PWD/Resources/Capture" "$APP/Contents/Resources/Capture"
cp "$PWD/Resources/Brand/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
mkdir -p "$APP/Contents/Resources/Legal/marked"
cp "$PWD/../../LICENSE" "$PWD/../../THIRD_PARTY_NOTICES.md" "$APP/Contents/Resources/Legal/"
cp "$PWD/../../third_party/marked/LICENSE" "$APP/Contents/Resources/Legal/marked/LICENSE"
codesign --force --sign - "$APP"
printf '构建完成：%s\n' "$APP"
