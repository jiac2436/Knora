# 工具栏图标占比优化 · 2026-10-30

版本0.1.2，仅替换 action.default_icon 指向的工具栏图标。保持现有 App 品牌几何与配色，从现有图稿去除中性黑色外壳及外圈留白，保留彩色双环和中央白点，再以约95%的画布占比导出透明16/24/32/48px PNG。通用扩展图标、插件页头、说明页及App图标均不变。

工具栏位置由浏览器固定为16DIP，提供24px支持1.5倍缩放、32px支持2倍缩放；不能通过增大PNG文件尺寸扩大按钮本身。依据Chrome官方action文档：https://developer.chrome.com/docs/extensions/reference/api/action 。

检查PNG尺寸、清单路径、JavaScript和权限白名单通过。通用图标摘要逐项匹配0.1.1验收记录；色彩主体宽度从16px版本的10px提升至15px，32px版本22px提升至30px，48px版本32px提升至46px。comparison.png是独立浏览器在深浅背景的实际像素对比示意，已人工检查，不代表操作了用户当前浏览器。没有修改采集、保存、设置、桥接或用户存储。

浏览器扩展管理页重新加载已有插件后生效，无需删除插件或清空数据。常规构建直接复制PNG；重新导出使用 generate-icons.py --toolbar-only，需macOS的sips和Xcode Command Line Tools。
