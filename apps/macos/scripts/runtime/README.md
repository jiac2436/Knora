# L0 运行环境验证

这些脚本用于隔离的打包实验，不生成正式安装包，不启动业务服务或读取用户知识库。

- `build_runtime.py`：从固定归档和摘要锁离线构建运行包，打包后端源码并生成完整清单；当前构建输入见[运行包说明](../../runtime/README.md)。
- `verify_runtime.py`：按外部可信清单摘要检查文件、权限、链接和额外文件。
- `test_runtime_tools.py`：归档路径、链接、清单篡改、输出保护及安装记录回归测试。
- `prepare_poc.py`：复制独立 Python、与其小版本匹配的已安装依赖和指定向量模型；拒绝虚拟环境作为解释器输入，拒绝覆盖已有目标目录，检查外部符号链接。依赖来自开发环境，仅作为可行性实验；发行版需要从锁定的制品重新安装。
- `probe.py`：逐子进程验证 SQLite/TLS、API、PDF 渲染、OCR 运行库、文档模块与本地向量推理。临时 HOME 和工作目录避免使用日常配置，Python 网络审计钩子阻止 Python socket 连接，但不替代系统网络隔离。OCR 检查不下载模型、不证明真实识别质量。
- `vendor_paddle.py`：仅允许修改带有 PoC 标记的候选目录；从显式提供的 arm64 GCC 运行库目录复制四个依赖，修正 Paddle 的加载路径并作临时签名。缺失输入先拒绝，正式发行签名和许可交付另行验收。
- `native_inventory.py`：扫描可执行文件和动态库的绝对加载路径，排除动态库自己的标识。输出用于人工审查；不校验所有 `@rpath`、签名或原生代码网络行为。

示例（所有路径替换为本次实验路径，目标目录必须不存在）：

```sh
python3 apps/macos/scripts/runtime/prepare_poc.py \
  --standalone /tmp/standalone-python \
  --site-packages .venv/lib/python3.10/site-packages \
  --model services/api/model-cache/fast-bge-small-zh-v1.5 \
  --destination /tmp/pka-runtime-candidate
/tmp/pka-runtime-candidate/python/bin/python3 -I -B \
  /tmp/pka-runtime-candidate/probe.py \
  --model /tmp/pka-runtime-candidate/model --output /tmp/runtime-before.json
```

随后移动整个候选目录，在新路径重复运行 probe；暂时隐藏本次下载的原解释器目录以排除回用。不要移动系统 Python 或项目开发环境。用 native_inventory 扫描搬迁后的 `python` 目录，绝对外部依赖需要单独处理，冒烟通过不表示可以发布。

完整实验记录见 [L0 验证记录](../../../../docs/L0运行环境验证记录.md)。

## 扩展验证

GCC 制品从官方元数据选择并验证摘要，只提取运行库到临时目录，不安装系统 GCC。来源及摘要见验证记录；不要将当前开发快照当成发行依赖锁。

```sh
python3 apps/macos/scripts/runtime/vendor_paddle.py \
  --candidate /tmp/pka-runtime-candidate --gcc-runtime /tmp/gcc-runtime
/tmp/pka-runtime-candidate/python/bin/python3 -I -B \
  /tmp/pka-runtime-candidate/probe.py \
  --model /tmp/pka-runtime-candidate/model \
  --ocr-models /tmp/pka-runtime-candidate/ocr-models \
  --native-math --output /tmp/runtime-functional.json
```

`--ocr-models` 指向包含 `PP-OCRv6_small_det_infer` 和 `PP-OCRv6_small_rec_infer` 的根目录，模型通过参数指定，不使用个人下载缓存。探针默认增加合成 DOCX/CSV 转换、文本 PDF 两条解析路径；可选的原生数学检查直接加载 BLAS/LAPACK 并验证矩阵结果及已加载 GNU 库。OCR 只验证固定合成样本，不代表识别质量验收。

可在 macOS 的 sandbox-exec 中用 `(version 1)(allow default)(deny network*)(deny file-read* (subpath "/opt/homebrew"))` 运行探针。使用前验证该限制确实拒绝访问；这种当前主机隔离仍不替代清洁设备安装验收。
