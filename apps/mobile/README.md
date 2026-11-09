# Mobile M1

Expo + React Native + TypeScript 的 M1 移动端基础界面，覆盖：

- 收集箱、上传/处理/覆盖状态与下拉刷新；
- 文字和链接保存，知识域、项目、收藏原因、个人备注与适用条件；
- 资料详情、Agent 可见性、个人上下文编辑；
- 追加实践结果与历史记录；
- 开发 API 地址和 Bearer token 配置。

## 启动

要求 Node.js 20+。进入本目录后执行：

```bash
cp .env.example .env.local
npm install
npm run typecheck
npm start
```

在 `.env.local` 配置 `EXPO_PUBLIC_API_BASE_URL` 和仅供开发的 `EXPO_PUBLIC_DEV_BEARER_TOKEN`。iOS 模拟器通常可以访问 `127.0.0.1`；Android 模拟器访问宿主机通常使用 `10.0.2.2`。真机应配置电脑在局域网内可访问的地址。

## M1 边界

当前版本直接向云端 API 保存文字或链接。以下能力属于 M2，不在本工程中模拟成功状态：

- iOS / Android 系统分享扩展；
- 设备持久离线上传队列；
- 图片、PDF、音频与录音；
- OCR、转写和生成式知识卡。

链接以 `link_only` 提交；文字以 `full_text` 提交。开发 token 会进入客户端运行环境，只能使用无生产权限、短期的本地开发凭证，生产认证需替换为安全会话流程。
