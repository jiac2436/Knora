# Knora 应用图标

当前图标采用维护者选定的 A 方案，以原有设计放大并加粗线条。正常客户端构建只复制预生成的 ICNS，不增加 Node 或图像处理运行依赖。

- `AppIcon-A-source.png`：当前 A 方案设计原图。
- `AppIcon.png`：1024 × 1024 RGBA 资源，保留交付所需透明边距。
- `AppIcon.icns`：macOS 多尺寸应用图标，也供客户端侧栏加载。
- `AppIcon.svg`：保留的原版矢量设计参考，不是生成当前 A 方案的源文件。

更新当前图标时应使用 A 方案原图导出标准 iconset，并用 macOS `iconutil` 生成 ICNS；不能将历史 SVG 误当成当前图标重建输入。素材来源与授权边界见[第三方声明](../../../../THIRD_PARTY_NOTICES.md)。
