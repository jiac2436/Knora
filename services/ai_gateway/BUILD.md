# 2026-10-20 共享模型执行器实施状态

> 当前 macOS App 已内置本机服务与模型执行器，不要求用户单独启动本文件中的服务。下方命令用于外置开发或历史排查；当前 Python 3.12 开发、依赖和测试入口见 [开发与验证](../../docs/开发与验证.md)，客户端构建见 [BUILD](../../apps/macos/BUILD.md)。

执行能力已拆为 `execution.py`、`settings.py`、`prompts.py`、`provider.py`、`errors.py` 和 `vision.py`。`main.py`仅保留可选HTTP适配器，内置Worker直接复用相同执行器，不新增网关监听端口或守护进程。HTTP开发入口的启动命令和环境配置仍保留。

M2桌面模型凭证改用私有启动通道，仅Worker安装供应商Key；独立HTTP开发网关仍使用原环境变量。提示词、版本、图文和响应校验保持。M5已完成同版整包、真实DeepSeek合成样本累计7次调用验收并发布，普通用户无需单独启动网关，模型直接在客户端配置。来源见 [实施记录](../../docs/verification/kernel-refactor-20261020/README.md)。历史“网关未内置”等文字只对应当时版本；下方启动命令用于显式外置开发模式。

# AI 网关构建与启动说明

## 2026-10-17 图文联合分析

继续使用项目 `.venv` 和既有依赖。网关需要与新版 API／Worker 共用图文 v3 契约，并包含 `vision.py`。保留当前安全配置的密钥与网关访问凭证，在原网关终端执行：

```bash
export PKA_AI_VISION_MODEL=deepseek-flash
bash services/ai_gateway/start.sh
```

已有进程先用 Ctrl+C 退出，再运行，不创建第二个占用8091的网关。样例变量文件不会自动加载。`PKA_AI_MODEL` 可以保留原文字模型，图片段使用视觉模型；最终合成仅使用分段结果，不重复上传原图。`GET /health` 新增 `analysis_image_support`，只读且不消耗模型用量；true代表配置存在，真实效果仍需实际合成图表验收。

图文请求最多32MiB，原图验证和长图切块在网关完成；普通分析、RAG和节点请求继续原有限制。网关不公开图片外链代理。上游HTTP失败安全诊断为 `provider_failed`，附 `upstream_status` 与固定 `category`；不记录或返回上游原始错误消息、密钥、正文、图片或思考文本。当前本机网关已重启且通过真实合成图表验收，不需要再重启。健康检查本身不能证明质量通过，具体范围见 [本轮验收](../../docs/verification/a1/joint-images-acceptance-20261017.md)。客户端也需完整更新，见 [macOS BUILD](../../apps/macos/BUILD.md)。下方日期小节为历史说明。

## 2026-10-17 问答正文不再输出内部 ID

问答提示词为 `original-evidence-answer-v1.1`：`answer` 只写正常可读内容，原文 ID 只返回独立 `citations`，由客户端展示来源。引用 ID 的完整性、白名单校验和缓存/不重发策略保留；兼容旧模型返回的有效 UUID 标记，客户端读取时转换为编号。重启原终端的 `bash services/ai_gateway/start.sh` 加载更新。没有新增调用、依赖、数据库字段或迁移，摘要提示词仍为 `domain-summary-navigation-v1.1`。验收见 [本轮记录](../../docs/verification/a1/readable-citations-acceptance-20261017.md)。

## 2026-10-17 引用兼容与安全诊断

当前摘要提示词 `domain-summary-navigation-v1.1`。每条引用建议 1～3 个、硬上限十个，响应协议固定 v1；请求 v2 并不表示结果也应返回 v2。v2 的最终和中间输出预算均为 8,192 token，正文仍受原有 500／800／2,200 字符上限约束，未引入自动重试或额外模型调用。API／Worker 也须切换当前版本。

原网关终端 Ctrl+C 后执行 `bash services/ai_gateway/start.sh` 加载修改；只读 `GET /health` 返回 `ready`、节点契约版本和摘要提示词版本，不需要凭证、不读取资料、不调用模型。概览输出被拒时，原网关终端会记录 `node_result_rejected`，仅包含请求 ID、安全原因、白名单字段和计数，不含正文或模型原始输出。诊断不需要重发真实请求。

仅 v2 节点信封允许最多 2 MiB，原文窗口只在本地／网关校验，不传给模型；供应商摘要输入预算仍为 48,000，旧 v1、分析与问答仍限 512 KiB。无新数据库或运行依赖。详细边界与验收见 [本轮记录](../../docs/verification/a1/navigation-citations-acceptance-20261017.md)。

## 2026-10-16 摘要优先概览更新

知识域更新默认按文档概览一次汇总，超出预算才分批，并显示实际步骤进度。请求新增兼容的 v2 路径，API／Worker 与独立网关均须加载本轮源码；网关重启命令仍为 `bash services/ai_gateway/start.sh`。数据库、原文和单篇结果格式不变，本轮未新增迁移。当前工作区已有回收站 schema v16，请以 `migrations.CURRENT_VERSION` 为准，不能将后续历史 v15 小节当作当前版本。

设计与边界见 [摘要优先概览设计](../../docs/design/知识域概览摘要优先优化-2026-10-16.md)。完整客户端需重新组装运行包，不能仅裸编译 Swift。真实模型质量验收仍需在切换版本后另行执行；普通回归不调用模型。


核验日期：2026-10-09。命令从仓库根目录执行。当前为独立DeepSeek开发网关，**不内置到macOS运行包，也不由App自动启动**。关闭客户端不会停止这个终端服务。

## 环境和凭证

复用项目 `.venv`及 `services/api/requirements.txt`，不用另建网关虚拟环境。启动前通过安全的进程环境配置以下变量；`.env.example`只是样例，服务不会自动读取它。

| 变量 | 用途 |
| --- | --- |
| `PKA_AI_PROVIDER_KEY` | DeepSeek密钥，仅供网关使用 |
| `PKA_AI_MODEL` | 明确选择的模型名；本机此前验收使用`deepseek-flash`，不是代码默认值 |
| `PKA_AI_GATEWAY_TOKEN` | API/Worker访问本网关的凭证 |
| `PKA_AI_PROVIDER_BASE_URL` | 上游地址，默认`https://api.deepseek.com` |

App本地凭证、Agent凭证、网关访问凭证、Provider Key是不同凭证。不要打印真实值，不写入仓库/构建产物/日志，不把Provider Key填到客户端“网关访问凭证”。

## 单进程运行

推荐使用启动脚本，避免漏写工厂函数：

```bash
bash services/ai_gateway/start.sh
```

脚本从任意目录定位仓库，使用项目 `.venv`，检查必需环境变量是否存在，继承当前终端的配置，不读取 `.env`，不输出密钥。重启时先在原终端按 Ctrl+C，再执行此命令。不要另开重复进程占用 8091。

等价完整命令如下；`main:create_app` 中的冒号和函数名不能省略：

```bash
PYTHONPATH=services/api:. .venv/bin/python -m uvicorn \
  services.ai_gateway.main:create_app \
  --factory --host 127.0.0.1 --port 8091 --workers 1
```

缺少必需配置时启动失败。API/Worker使用`http://127.0.0.1:8091`及相同网关Token；完整本地客户端通过“设置→AI分析”保存地址与网关凭证，App启动时传给自己管理的服务。开发API/Worker的终端变量不能自动配置完整客户端。

开发重载可用：

```bash
PYTHONPATH=services/api:. .venv/bin/python -m uvicorn \
  services.ai_gateway.main:create_app \
  --factory --host 127.0.0.1 --port 8091 \
  --reload --reload-dir services/ai_gateway --reload-dir services/api/app
```

第二个监视目录使共享契约变更也能加载；它同时会因其他API源码变更而重启网关。只监视网关目录时，改`ai_contract.py`/`rag_contract.py`后必须手动重启。不要同时使用`--reload`和多Worker，环境变量改变后也需重新启动。

网关请求缓存仅存内存，任何重载都会清空。先完成当前请求诊断，再重启；不能为了查看缓存而重发已经因重启失去缓存的请求，这可能再次计费。

## 校验与故障定位

```bash
# 不带访问凭证，只检查监听与鉴权；预期401，不调用模型
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8091/v1/analyze
PYTHONPATH=services/api:. .venv/bin/python -m pytest \
  services/api/tests/test_ai_gateway.py services/api/tests/test_rag_gateway.py -q
```

查看启动网关的原终端Uvicorn日志。`POST /v1/analyze`和`POST /v1/answer`才是业务模型调用；不使用真实请求作为无成本健康检查。401先检查调用方与网关的访问凭证；502等错误需结合安全诊断，不能直接判为密钥错误。

真实DeepSeek验收会消耗模型用量，范围和授权按具体任务处理；模拟测试不产生真实费用。协议、缓存上限、错误诊断和当前非生产计费边界见 [README](README.md)。远程部署需要HTTPS及正式认证，不能把本地命令直接暴露公网。

修改网关代码只需更新并重启网关；修改共享契约或Worker消费逻辑还需更新调用方，随包模式必须重新组装运行包。API/Worker运行包构建不会把这个网关进程升级或启动。
