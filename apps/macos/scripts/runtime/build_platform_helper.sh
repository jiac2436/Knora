#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/../../../.."
OUTPUT="${1:?请指定系统辅助程序的绝对输出路径}"
[[ "$OUTPUT" == /* && ! -L "$OUTPUT" && "$OUTPUT" != "$PWD/apps/macos/dist/"* ]] || exit 1
mkdir -p "$(dirname "$OUTPUT")" apps/macos/.build/module-cache
xcrun swiftc -parse-as-library -O -target "$(uname -m)-apple-macosx14.0" \
  -module-cache-path apps/macos/.build/module-cache \
  apps/macos/Sources/PlatformContracts.swift apps/macos/Sources/LocalLibrary.swift \
  apps/macos/Sources/MacLibraryStore.swift apps/macos/Sources/MacPaths.swift \
  apps/macos/Sources/MacPrivateFiles.swift apps/macos/Sources/MacRestrictedCommands.swift \
  apps/macos/Sources/MacImageTextRecognizer.swift apps/macos/scripts/runtime/MacPlatformHelper.swift \
  -o "$OUTPUT"
codesign --force --sign - "$OUTPUT"
