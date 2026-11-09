# 模型执行器开发

桌面 App 的直接模式已内置本目录代码，不需要用户单独启动网关。完整客户端构建见 [macOS BUILD](../../apps/macos/BUILD.md)，Python 环境与测试见[开发与验证](../../docs/开发与验证.md)。

## 外部 HTTP 调试入口

只有明确选择外部网关开发模式时才运行。以下配置从当前终端环境读取，`.env.example` 不会自动加载：

| 变量 | 用途 |
| --- | --- |
| `PKA_AI_PROVIDER_KEY` | 上游模型 API Key，必填 |
| `PKA_AI_MODEL` | 文字模型名称，必填 |
| `PKA_AI_GATEWAY_TOKEN` | API/Worker 访问本网关的独立凭证，必填 |
| `PKA_AI_PROVIDER_KIND` | `deepseek` 或 `openai-compatible`，默认前者 |
| `PKA_AI_PROVIDER_BASE_URL` | 上游 HTTPS 基础地址，默认 `https://api.deepseek.com` |
| `PKA_AI_VISION_MODEL` | 可选视觉模型，需满足对应接口的输入要求 |

不要把模型 Key 和本网关的访问凭证混用，也不要将真实值写进仓库、日志或聊天。准备好环境变量后，从仓库根目录运行：

```bash
bash services/ai_gateway/start.sh
```

脚本使用项目 `.venv`，只监听 `127.0.0.1:8091`，检查必要变量但不打印密钥。API/Worker 需使用相同网关凭证及 `PKA_AI_GATEWAY_URL=http://127.0.0.1:8091`；终端环境不会自动配置已安装的桌面 App。

`GET /health` 为只读检查，不调用模型。重启时在原终端按 Ctrl+C，再执行启动命令；不要同时启动第二个占用相同端口的进程。重启会清除内存请求缓存，不能据此自动重发结果未知的收费请求。

修改共享契约、Worker 或内置执行器时，随包模式必须重新组装对应运行包。不要把本地调试命令直接暴露到公网。
