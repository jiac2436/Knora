# 当前应用图标：A 方案

2026-10-23 用户选定第二轮 A 方案：以此前第二版为基础放大并加粗。当前使用 `AppIcon-A-source.png` 作为设计原图，`AppIcon.png` 和 `AppIcon.icns` 为带 5% 透明边距的 macOS 交付资源；客户端左上角也从同一 ICNS 加载。

可复现生成脚本及验收见 `docs/verification/icon-a-20261023/`。本次只替换图标资源，保留原图配色、造型和亮边；正常客户端构建仍只复制预生成 ICNS。

## 历史原版矢量源稿

`AppIcon.svg` 保留为历史原版设计源稿，不再用于生成当前 A 方案。下列说明仅对应历史原版：

采用用户设计稿第 11 款「Vortex 旋转透视阵列」，保留原始路径、渐变、旋转角度及透明留白。`AppIcon.svg` 只提取该款引用的四个渐变定义；没有采用后续美化方案。

- `AppIcon.svg`：矢量源稿，200 × 200 viewBox。
- `AppIcon.png`：1024 × 1024 RGBA 预览。
- `AppIcon.icns`：macOS 多尺寸应用图标，同时供侧栏加载；构建时复制到 Bundle Resources，Info.plist 的 CFBundleIconFile 指向此文件。

更新时，从 SVG 导出标准 iconset：icon_16x16、icon_32x32、icon_128x128、icon_256x256、icon_512x512，每项各含 1x / @2x PNG，保留透明度。用 `iconutil -c icns <目录>.iconset -o AppIcon.icns` 打包。这次使用本机已有 Sharp 渲染 PNG；正常客户端构建只复制已生成的 ICNS，不增加 Node 或图像处理依赖。
