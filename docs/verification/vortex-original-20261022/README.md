# 第 11 款原版 Logo 部署验收

用户确认采用原版 Vortex，未采用 A/B/C 美化版本。本次仅变更品牌资源、Workspace 左上标识及应用图标打包配置，不改客户端名称、功能或用户资料。

## 交付内容

- `apps/macos/Resources/Brand/AppIcon.svg` 精确提取原稿第 11 款的五个图形及其四个渐变；逐属性 XML 比对通过。
- PNG 保留原稿透明边距，ICNS 包含标准十项 1x / @2x 表示，实际像素范围 16–1024。
- `CFBundleIconFile` 指向随包 `AppIcon.icns`。左侧品牌标读取同一资源；去掉旧“知”字和额外背景。
- 构建脚本复制预生成资源，不增加构建或运行依赖。

## 验证

完整优化构建、deep / strict 签名、同版 runtime 19957 个文件校验通过。与上一正式发布记录逐文件比较，Swift 仅 `Workspace.swift` 改动。候选包 ICNS 与生产资源 SHA256 一致。

`IconResourceCheck.swift` 使用真实 AppKit / Bundle，验证资源定位、原生解码、正方形及 Alpha，得到 10 项表示：[16, 32, 32, 64, 128, 256, 256, 512, 512, 1024]。新渲染 PNG 已离线目视检查。

```bash
xcrun swiftc docs/verification/vortex-original-20261022/IconResourceCheck.swift \
  -module-cache-path apps/macos/.build/module-cache -o /tmp/pka-icon-verify
/tmp/pka-icon-verify apps/macos/dist/知识收集箱.app
codesign --verify --deep --strict apps/macos/dist/知识收集箱.app
```

构建保持既有 Security 弃用警告，不涉及本次改动。未主动启动用户 App、访问真实知识库或调用模型；未强制重启 Dock / Finder 刷新系统缓存。发布来源与归档位置以 `apps/macos/release.json` 为准。

## 发布结果

预检与发布通过，正式包再次通过原生图标加载、严格签名和二进制／资源摘要核对；dist 仅有一个 App。旧包归档于 `apps/macos/archive/20261022-132116`。
