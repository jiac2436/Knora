# 按钮样式统一

## 原因与范围

浅紫强调按钮和灰色普通按钮表达操作层级，颜色不应全部相同。问题是部分页面仍使用系统 automatic / borderedProminent，而主界面使用 InboxButtonStyle，造成高度、圆角、颜色及悬停反馈混杂。真实隔离窗口进一步验证：给主窗口设置按钮样式，不会自动应用到其 `.sheet` 的普通按钮。

修复使用现有 InboxButtonStyle：主工作区给普通按钮设置 secondary 默认值，各 sheet 内容边界显式设置相同默认值；新建知识库／项目的 popover 同样设置。4 处系统 borderedProminent 改用项目 primary。现有 plain、图标、列表、文本链接等专用样式保持显式覆盖。系统警告确认框与系统菜单保留平台原生呈现。

层级约定：primary 为浅紫底深色文字；secondary 为灰色底；quiet 用于辅助操作；destructive 保留红色文字。未改变操作、确认流程、任务状态或数据。

## 验证方式

`ButtonStyleRenderingTest.swift` 使用生产 Design.swift，通过 SwiftUI ImageRenderer 检查：普通按钮继承 secondary；primary 和 plain 显式覆盖不受影响；禁用状态仍有视觉区别。四项检查通过，输出样张见同目录 PNG。

```bash
xcrun swiftc -parse-as-library -target arm64-apple-macosx14.0 \
  -module-cache-path apps/macos/.build/module-cache \
  apps/macos/Sources/Design.swift apps/macos/scripts/ButtonStyleRenderingTest.swift \
  -o /tmp/pka-button-style-test
/tmp/pka-button-style-test /tmp/pka-button-style-render
```

原生界面验收复用 LocalDomainOverviewUITest 的隔离 Bundle、合成内容及内存传输。测试入口显式设置与真实工作区相同的样式和主题，避免使用用户库或发起真实模型调用。

## 原生界面结果

通过 CUA 在最终隔离测试包打开概览及嵌套全量分析窗口，确认“完成”“重新预览”和“向这个知识域提问”均使用统一灰色样式，“全量分析”及“开始全量分析”使用统一浅紫主样式；Esc 关闭嵌套窗口并返回概览。截图分别为 overview-buttons.png、overview-question-button.png、full-analysis-sheet.png。未点击提交分析，真实模型调用为 0。

## 发布结果

用户确认退出后，发布预检及正式部署通过。正式包再次通过 deep / strict 签名、唯一 App 入口、二进制摘要及原版 Vortex 图标摘要核对；同版运行包保持不变。旧包归档于 `apps/macos/archive/20261022-134855`。发布来源记录于 `apps/macos/release.json`。
