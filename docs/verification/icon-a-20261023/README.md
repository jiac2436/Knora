# A 方案图标替换

用户选定第二轮 A 方案，即在此前第二版基础上放大、加粗的版本。用户确认退出客户端后已正式发布。

- 原图：`apps/macos/Resources/Brand/AppIcon-A-source.png`，保留选定图案及颜色。
- `GenerateIcon.swift` 用系统 CoreGraphics 打包标准透明边距及外轮廓圆角，不重绘图案；生成 1024px PNG 与十项 1x／@2x iconset，经系统 iconutil 输出 ICNS。
- 当前应用和左上品牌标识共用 `AppIcon.icns`，无 Swift 或后台变更。`AppIcon.svg` 保留为历史原版设计稿，当前资源来源已在品牌目录说明。
- 以当前已验收小红书阅读版克隆独立候选，只替换 ICNS 并重新签名；未混入共享工作区的其他开发改动。

## 验证

- 原生 Bundle／AppKit 检查通过：10 项可解码表示，实际像素 16–1024，全部含 alpha。
- 1024px 图标目视检查通过，保留 A 方案造型及加粗效果，具备透明边距。
- 完整逐文件比较仅 ICNS、主程序签名及 CodeResources 改变；主程序去签名后与上一正式版逐字节一致。
- 运行包 19959 文件按既有可信 manifest 校验通过，数据库 schema16、插件1.0.5保持。
- `codesign --verify --deep --strict` 通过。
- 发布预检检测到客户端仍在运行，依 `apps/macos/RELEASE.md` 等待用户结束任务并退出；未强制结束客户端或 Worker。

候选位置：`apps/macos/.build/icon-a-candidate/知识收集箱.app`。验收来源、资源及二进制摘要见 `runtime-icon-a.json`；前版交付记录见 `baseline-release.json`。

## 发布结果

用户明确回复“已退出，可以更新”后，预检与正式发布通过。正式包再次通过原生图标十项解码、deep／strict签名及资源／二进制／运行清单摘要验证，dist仅一个App。旧版归档于 `apps/macos/archive/20261023-190656`。未主动启动真实知识库，未强制刷新 Dock／Finder 缓存。
