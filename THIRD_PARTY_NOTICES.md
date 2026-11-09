# 第三方组件与许可

Knora 原创源码采用 [GPL-3.0-only](LICENSE)。第三方代码、模型及标识保留各自的版权和许可证。分发二进制时，应同时提供对应版本源码、依赖锁、构建脚本和下列许可材料。

## Marked 阅读引擎

阅读器内嵌 **Marked 15.0.12** 的官方 UMD 文件，公开源包为 [marked-15.0.12.tgz](https://registry.npmjs.org/marked/-/marked-15.0.12.tgz)，下载时核对 npm 的 SHA-512 完整性值。原文嵌入 Swift raw string，仅统一末尾换行，不修改库逻辑。

[许可文本](third_party/marked/LICENSE)包含 Marked MIT 许可及历史 Markdown 版权声明；[来源与摘要](third_party/marked/sources.json)记录精确文件。此前无版本标记的副本已被替换，不对旧副本作未经核验的版本推断。

## Python 与依赖

运行包固定 **CPython 3.12.16 / python-build-standalone 20261026**。保留包内 `python/lib/python3.12/LICENSE.txt`，并附上官方构建仓库的[许可集合与来源摘要](third_party/python-standalone/sources.json)。该上游集合包含不同构建配置的许可，并不表示所有列出的组件都被当前 App 使用。

基础 API、Worker、模型执行器及可选扩展涉及 FastAPI、Starlette、Pydantic、SQLAlchemy、MCP、Pillow、PDFium/pypdfium2、FastEmbed、ONNX Runtime、Paddle、OpenCV 及其传递依赖。固定版本和制品摘要见 [运行输入](apps/macos/runtime/README.md)。构建时保留各 wheel 的原始许可、NOTICE、第三方声明及原有 SBOM，不人工删减这些材料。

每次拆分会生成基础包 `dependency-inventory.json` 和 `dependency-inventories/{semantic,ocr,documents}.json`，列出实际依赖版本、声明许可、项目来源及保留许可文件摘要。部分 wheel 遗漏许可文本，已从同版本源码包或官方许可文件中[补充保留](third_party/python-supplements/sources.json)；补充记录明确区分同版本源包与单独取得的上游许可文本。

这些自动记录用于版本和材料核对，不等同于独立法律意见或对所有上游元数据准确性的担保。依赖或构建输入变化后应重新审查，不能沿用旧版本的合规结论。

## 本地模型与 OCR 原生库

- **BAAI/bge-small-zh-v1.5**：上游模型卡声明 MIT，保留 [FlagEmbedding 许可](third_party/models/BGE-LICENSE)及[来源](third_party/models/sources.json)。当前 ONNX 文件从固定 Qdrant/ModelScope revision 下载，准确摘要见 `artifacts.json`；FastEmbed 的独立许可同时保留。
- **PP-OCRv6 Small**：来自 Paddle 官方模型源，保留 [PaddleOCR Apache-2.0 许可](third_party/models/PaddleOCR-LICENSE)和[来源记录](third_party/models/paddle-source.json)；模型文件摘要固定在 `artifacts.json`。
- **GCC 16.2.0 运行库**：OCR 使用 libgfortran、libgomp、libquadmath、libgcc 等原生库。构建从固定 Homebrew bottle 提取，保留 `COPYING`、`COPYING.LIB`、`COPYING.RUNTIME` 和 GPLv3 正文。对应上游源码可从 [GNU GCC 16.2.0](https://ftp.gnu.org/gnu/gcc/gcc-16.2.0/) 获取，具体构建输入及来源保存在运行包 `build-inputs.json`。本项目对候选库修正了相对加载路径并作 ad-hoc 重签名；不修改其算法。

基础 App 不携带上述可选模型或 OCR 推理库。许可索引和补充文本随基础运行包保留；独立扩展按实际闭包保留各自 wheel 的原始许可。对外分发 App 与扩展时，应保持配套的许可材料及源码可获得。

## 产品素材与第三方标识

当前图标来自维护者提供并选定的原设计及后续 A 方案；[品牌资源说明](apps/macos/Resources/Brand/README.md)保留来源与生成过程。熊猫素材为已确认的 AI 生成图片，见[采集素材说明](apps/macos/Resources/Capture/README.md)。仓库中的这些产品素材随项目许可提供，但不承诺 AI 生成素材具有排他著作权。

Codex、Claude、飞书、小红书及其他第三方名称或标识仅用于兼容和来源识别，不表示其对本项目背书，也不授予相关商标权。第三方标识不属于 Knora 的原创素材。
