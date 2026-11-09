# 从源码构建 Knora

当前构建目标为 Apple Silicon、macOS 14.5+。需要 Xcode Command Line Tools、Python 3.12（构建脚本也可使用 Python 3.10+）和网络。所有命令从仓库根目录执行；不要复制已有 App、开发虚拟环境或用户扩展作为依赖输入。

完整路径是：**固定输入 → 全运行包 → 轻量基础包与三个扩展 → 下载清单 → App → 验收 → DMG**。历史热修和已删除的本机缓存不能用于从零构建，旧记录已移至 [BUILD-HISTORY](BUILD-HISTORY.md)。

## 1. 独立构建工具与输入

以下目录只用于构建，可在产物验收后清理。`KNORA_STAGE` 必须位于 `apps/macos/.build` 内，且最终输出目录尚不存在。

```bash
export KNORA_STAGE="$PWD/apps/macos/.build/release"
python3 -m venv "$KNORA_STAGE/tools"
"$KNORA_STAGE/tools/bin/python" -m pip install \
  uv==0.7.6 packaging==26.3 certifi==2026.10.18
export KNORA_BUILD_PYTHON="$KNORA_STAGE/tools/bin/python"

"$KNORA_BUILD_PYTHON" -B apps/macos/scripts/runtime/download_inputs.py \
  --output "$KNORA_STAGE/inputs"
"$KNORA_BUILD_PYTHON" -B apps/macos/scripts/runtime/verify_dependency_sources.py \
  --cache "$KNORA_STAGE/wheels" --output "$KNORA_STAGE/audit"
```

`artifacts.json` 固定 Python 3.12.16、模型和 GCC 原生库的来源、大小和摘要。wheel 版本及所有允许摘要固定在 `requirements-macos-arm64.lock`；脚本核对 PyPI 元数据，再实际下载清华或阿里镜像中的同一文件。来源不可用或校验失败会停止，不降级为未固定的最新版。

BGE 模型按固定 ModelScope revision 逐文件获取；OCR 使用固定的 Paddle 模型归档。没有私有 CDN，也不要求用户提供下载地址。

## 2. 离线构建并拆分

```bash
"$KNORA_BUILD_PYTHON" -B apps/macos/scripts/runtime/build_runtime.py \
  --inputs "$KNORA_STAGE/inputs" --wheelhouse "$KNORA_STAGE/wheels" \
  --uv-cache "$KNORA_STAGE/uv-cache" --uv "$KNORA_STAGE/tools/bin/uv" \
  --output "$KNORA_STAGE/full"
export KNORA_FULL_SHA="$(shasum -a 256 "$KNORA_STAGE/full/manifest.json" | awk '{print $1}')"
"$KNORA_BUILD_PYTHON" -B apps/macos/scripts/runtime/split_runtime.py \
  --source "$KNORA_STAGE/full" --manifest-sha256 "$KNORA_FULL_SHA" \
  --backend-source "$KNORA_STAGE/full/backend" --output "$KNORA_STAGE/split"
"$KNORA_BUILD_PYTHON" -B apps/macos/scripts/runtime/finalize_extensions.py \
  --runtime "$KNORA_STAGE/split/base" --artifacts "$KNORA_STAGE/split" \
  --inputs "$KNORA_STAGE/inputs" --wheels "$KNORA_STAGE/wheels" --audit "$KNORA_STAGE/audit"
```

构建器只采集当前 API、Worker、模型执行器和 Agent Skill 源码，不采集数据库、上传目录、测试输出或用户配置。拆分按真实依赖闭包执行，不能只凭包名人工删除二进制库。基础包保留云端 AI，模型及 OCR/向量推理依赖放到可选扩展中。

扩展 1.1.0 使用 CPython 3.12 ABI，与旧的 1.0.0 / CPython 3.10 扩展不兼容。升级后需重新安装配套扩展，原件、已保存分析和旧扩展文件不会因此删除。详见 [兼容说明](../../CHANGELOG.md)。

## 3. 独立运行验证与 App

```bash
"$KNORA_STAGE/full/python/bin/python3" -I -B "$KNORA_STAGE/full/probe.py" \
  --model "$KNORA_STAGE/full/model" --ocr-models "$KNORA_STAGE/full/ocr-models" \
  --native-math --output "$KNORA_STAGE/runtime-probe.json"
"$KNORA_STAGE/split/base/python/bin/python3" -I -B \
  apps/macos/scripts/lightweight_extensions_test.py "$KNORA_STAGE/split/base" "$KNORA_STAGE/split"
"$KNORA_STAGE/split/base/python/bin/python3" -I -B \
  apps/macos/scripts/lightweight_runtime_test.py "$KNORA_STAGE/split/base"

export PKA_RUNTIME_PAYLOAD="$KNORA_STAGE/split/base"
export PKA_RUNTIME_MANIFEST_SHA256="$(shasum -a 256 "$PKA_RUNTIME_PAYLOAD/manifest.json" | awk '{print $1}')"
PKA_APP_OUTPUT="$KNORA_STAGE/candidate/Knora.app" bash apps/macos/scripts/build.sh
codesign --verify --deep --strict "$KNORA_STAGE/candidate/Knora.app"
```

本次本机构建得到的摘要用于核对本次候选；验证别人提供的运行包时，必须使用可信发布记录中的摘要，不能信任待验证目录自报的 hash。

还应执行 [开发回归](../../docs/开发与验证.md)、真实 WKWebView/PDFKit 验证、随包备份恢复和全新用户目录启动。模型传输用合成响应验证，不默认调用付费模型。运行清单与许可证记录不能替代签名、公证或跨设备安装验收。

## 4. 发行边界

`build.sh` 默认构建到隔离候选，禁止直接写入 `dist`。不要在构建时覆盖正在运行的正式 App；验收后由维护者按 [RELEASE](RELEASE.md) 审查并替换。制作 DMG 见 [PACKAGING](PACKAGING.md)。

App 内包含 GPL、第三方声明、固定 Marked 许可、Python 上游许可集合，以及基础包/各扩展依赖的许可索引；各 wheel 原有许可文件保留。模型、原生库与补充许可来源见仓库 `third_party` 及固定输入。发布二进制时须一并提供对应源码、依赖锁、构建脚本与必要许可材料。

目前默认签名为本机 ad-hoc，尚无 Developer ID 签名和 Apple 公证。最低系统声明为 14.5；只在当前开发设备通过测试，不代表所有 14.5+ 设备都完成了实机验证。
