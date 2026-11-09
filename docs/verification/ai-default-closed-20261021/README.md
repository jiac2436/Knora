# AI 概览默认关闭与静默状态读取

此前每次选文都会用新的 sourceLoadGeneration 校验当前资料的 AI 状态，读取成功前为 loading。顶部按钮将该状态渲染为旋转圆弧，加上概览默认可见，造成切换文章时顶部按钮和概览标题同时转圈。这是读取已保存的任务/结果状态，并未创建新的 AI 分析。

本次改为：

- AI 概览默认关闭；已有结果也不自动打开，切换文章后恢复默认关闭。
- 顶部按钮在普通状态读取期间使用静态图标，保持后台读取和旧响应隔离。
- 用户明确打开概览后仍可看到状态读取提示或真实分析进度。
- 保留显式点击显隐、详情箭头、选中底色、原生动画与分析前确认。

未改数据库、API、凭证或运行包契约，无需迁移。验证使用合成资料和模拟接口，不调用真实模型；实际结果见同目录验收 JSON 与运行日志。

## 验证结果

最终完整原生构建和合成界面回归通过，最终日志为 `test-output.txt`，编译输出为 `compile-output.txt`（仅测试辅助功能桥接的一条弃用警告）。实际 NSAccessibility 按钮动作验证了首次默认关闭、恢复已有结果不自动打开、明确点击仅开标题、独立详情箭头及处理中显隐。旧响应隔离、确认流程与宽窄正文位置回归通过。

首次视觉检查发现：AX 节点已移除时，旧浮层仍在做淡出，造成读取提示残影。修复将切文重置放入禁用动画的 Transaction，用户主动开合动画保留。新增切文 50 ms 像素检查：修复前残影 RGB(32,32,32)/RGB(31,31,31)，修复后与稳定隐藏背景 RGB(24,24,24) 相同。修复前证据为 `switch-expanded-before-fix.png`、`switch-title-before-fix.png`；`test-output-before-visual-check.txt` 只记录了首轮不足以检查残影的 AX 回归，不能作为最终验收。

最终 `startup-loading.png`、`startup-restored.png`、`switch-1-loading.png`、`switch-2-loading.png` 已查看：默认无概览、无顶部旋转圈，切文不残留旧层。显式打开截图为 `startup-explicit-open.png` 与 `switch-*-explicit-open.png`。

## 正式发布

已更新 `apps/macos/dist/知识收集箱.app`。正式包签名、源码和二进制摘要、版本记录与唯一入口核对通过；旧包归档于 `apps/macos/archive/20261021-184305`。
