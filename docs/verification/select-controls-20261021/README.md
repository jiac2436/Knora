# 统一下拉控件验收

本次验证共享 `InboxSelect` 的选择行为、边界与视觉布局。使用纯合成 SwiftUI Binding 和临时窗口，不启动后台、不读取真实知识库、不调用模型。生产页面验收使用临时目录的 `CaptureModel`，不连接服务、不点击保存。

## 验收结果

完整 Sources 编译的测试程序退出码为 0，完整输出见 run.log（历史产物已清理）。

- 字段的真实辅助功能命中区域不小于 38 pt；包含说明的选项行不小于 44 pt（实际为 56 pt）。
- 真实辅助功能按压打开字段、选择短列表选项、关闭，并核对 Binding；同一字段可连续重新打开选择不同条目。
- 长中文名称可选择，完整值保留；窄布局中选项分两行展示，触发字段单行截断。
- Esc 关闭而不改写选择；Up + Return、聚焦字段 Space 打开、Down + Space 选择均通过。
- 搜索输入框聚焦时，Down + Return 选择过滤后的下一项；选项原始索引和值不会因过滤错位。
- 长列表高度有界；选择第 20 项后重开，已选条目完整滚入可见区，筛选文本重置为空。
- 无匹配结果时按回车不改写 Binding。
- 禁用和空选项不改值；Optional Binding 的 `nil` 与非空值均可选择。
- 打开的动态列表从 `[one,two]` 更新到 `[two,three]` 后可选择新增条目；移除当前值时字段显示占位，业务 Binding 不被自动改写；清空列表后关闭弹窗。
- 实际 `SettingsView` 的“读取方式”使用共享样式，实际选择更新 `model.webReadEngine`；未执行保存操作。

自动验收等待原生 popover 的展示/关闭完成后再执行下一次辅助功能操作（350 ms），避免前一次关闭过渡期间抢焦点。辅助功能遍历保留本次访问的虚拟节点强引用，避免地址复用导致误判。测试键盘事件只通过合成窗口的 `sendEvent` 投递，不向系统或其他应用注入输入。

## 已查看的截图

- 生产设置页面（历史产物已清理）、生产设置选项（历史产物已清理）
- 字段及禁用状态（历史产物已清理）、短列表（历史产物已清理）
- 长中文名称（历史产物已清理）、窄布局字段（历史产物已清理）、窄布局选项（历史产物已清理）
- 长列表筛选（历史产物已清理）、第 20 项重开可见（历史产物已清理）、无匹配结果（历史产物已清理）
- 动态项更新前（历史产物已清理）、动态项更新后（历史产物已清理）

截图从合成进程自己的 AppKit contentView 生成。主页面与原生 popover 是独立窗口，因此分别保存；图中没有用户资料。生产设置、短列表、长中文、窄布局、筛选与第 20 项可见区已目视检查。

## 复现

```sh
xcrun swiftc -D CAPTURE_QUEUE_TESTS -parse-as-library \
  -target arm64-apple-macosx14.0 \
  -module-cache-path apps/macos/.build/select-test-module-cache \
  apps/macos/Sources/*.swift apps/macos/scripts/SelectControlTest.swift \
  -o /tmp/pka-select-control-test
```

原生窗口验收需要可访问 WindowServer 的测试 App bundle。将上述可执行文件作为临时 `SelectControlTest.app/Contents/MacOS/SelectControlTest`，bundle id 使用 `local.pka.select-control-test`，然后：

```sh
PKA_SELECT_PREVIEW="$PWD/docs/verification/select-controls-20261021" \
  /tmp/SelectControlTest.app/Contents/MacOS/SelectControlTest
```

唯一编译警告来自测试进程启用 `AXEnhancedUserInterface` 的旧式 AppKit 桥接 API，与现有原生验收方式一致，不改变系统辅助功能设置。

## 范围与限制

已验证新共享组件及实际网页读取设置字段。未逐一打开所有替换位置，未自动化物理鼠标 hover、滚轮手势、系统 VoiceOver；键盘选中导致滚动与滚动边界已通过真实 AppKit 窗口和截图验证。本记录不代表已发布正式 App，发布由根任务统一执行。

## 实施范围与设计

15 处非分段选择器已统一接入：采集知识库/项目，网页读取、PDF引擎/模型、模型接口，标签目录与资料标签、主题父级、Agent 客户端、问答会话和版本。分段阅读切换与命令菜单沿用原有行为。

选择框固定38点高，标签上置，选项44点高（带说明56点）；整行命中，长列表滚动区域上限288点，12项及以上出现筛选。使用现有 InboxStyle 的石墨灰 canvas/surface、浅紫 accent、13点系统正文字和12点辅助标签；选中项以浅紫底色、勾选与轻描边强调。系统原生 popover 负责定位及点击外部关闭，列表内容自行绘制。

参考 [Apple Pop-up buttons](https://developer.apple.com/design/human-interface-guidelines/pop-up-buttons) 与 [Popovers](https://developer.apple.com/design/human-interface-guidelines/popovers)。没有新增依赖，数据库/API/运行包契约未改，无需迁移。

## 正式发布

已更新唯一正式 App `apps/macos/dist/知识收集箱.app`。正式包签名、源码和二进制摘要、版本记录与唯一入口校验通过。旧包归档于 `apps/macos/archive/20261021-191146`。
