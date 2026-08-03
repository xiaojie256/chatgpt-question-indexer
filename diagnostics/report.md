# 无障碍接口调查报告

诊断时间：2026-07-29；附件消息复诊：2026-08-03（Windows 10 10.0.19045）

## 实测对象

- 主窗口标题：`ChatGPT`
- 进程：`ChatGPT.exe`
- 诊断时 PID：`32780`
- 主窗口 HWND：`2237034`
- 外层框架：Win32 `Chrome_WidgetWin_1`
- 内层文档：Chrome `Document`，AutomationId 为 `RootWebArea`，name 为 `Codex`

PID 和 HWND 只记录本次诊断结果；程序运行时会重新枚举窗口，不使用固定 PID、安装路径或屏幕坐标。

## 三种视图调查

- 首次诊断（全屏布局）：control/raw 视图先只暴露 13/12 个外层框架节点，content 视图通过嵌套 `RootWebArea` 暴露 293 个节点。
- 最终复诊（侧栏贴靠后的窄布局）：control/raw/content 遍历分别得到 619/618/618 个节点。
- 节点包含 `Document`、`Group`、`List`、`ListItem`、`Text`、`Edit`、`Button` 等。布局变化会令 Chrome UIA 树重建，因此程序每次扫描和跳转都会重新定位窗口、文档和滚动容器，不缓存活动 wrapper。

完整逐节点字段（role、name、value、description、automation id、rectangle、pattern/action）见 `accessibility-tree.txt`。

## 根据真实树确定的规则

1. 对话滚动容器：
   - role：`Group`
   - class 包含：`thread-scroll-container`
   - 支持：`Scroll`、`ScrollItem`
   - 实测 `CurrentVerticallyScrollable = true`
   - 实测垂直百分比约 `99.6845`
   - 实测可视范围约 `78.0319%`

2. 用户消息：
   - 容器的直接子节点出现隐藏 `Text`，name 为 `You said:`
   - 紧随其后的 `Group` 为用户消息气泡
   - 气泡的后代 `Text`/`Edit` 叶节点按 UIA 顺序拼接成完整正文
   - 用户气泡支持 `ScrollItem`，可调用 `ScrollIntoView`

3. 助手消息：
   - 由隐藏 `Text`，name 为 `ChatGPT said:` 标记
   - 该标记后的内容不会加入提问目录

### 2026-08-03 附件消息复诊

- 实测对话：`AI编程痕迹`，control/raw/content 均约 663–664 个可访问节点。
- 一条用户消息的直接子节点顺序为：`你说：`、5 个 `composer-attachment-surface` Button、正文 Group、复制/编辑 Button。
- 正文 Group 内的 Text 为 `存在 AI编程的痕迹吗`，系统无障碍接口可完整读取，无需 OCR。
- 旧版仅查看角色标记后四个节点，因此在附件超过三个时无法到达正文 Group。
- 新规则持续查找到下一条用户/助手语义标记，跳过附件 Button，并将第一个正文 Group 作为消息气泡。
- 实测连续三次完整扫描均得到 1 条正文提问，约 2.18–2.27 秒；扫描前后滚动百分比均为 `32.298%`。
- 实测跳转模式为 `percentage+refine`，约 0.244 秒定位到正文，随后测试恢复到原滚动位置。

4. 虚拟化处理：
   - 本次树在当前会话中暴露了屏幕外节点，但程序不依赖这一行为。
   - 全量扫描会记录原百分比，从 0% 分页滚到 100%，每页重新获取容器和子节点，以消息文本 SHA-256 去重，最后恢复原百分比。

5. 跳转：
   - 首选通过记录的 UIA runtime id 在用户消息气泡中直接定位并调用 `ScrollIntoView`
   - runtime id 失效时才重新计算消息文本哈希
   - 失败时设置保存的垂直百分比
   - 再次按文本哈希复核，若找到则调用 `ScrollIntoView` 微调

## 性能复测

- 移除了每 1.8 秒执行的窗口枚举、`SW_RESTORE` 和 `SetWindowPos`。
- 启动或用户点击 `↔` 时仅贴靠一次；改变 Codex 尺寸并等待 3 秒后，窗口和侧栏坐标均未被改写。
- 主窗口句柄通过 Win32 枚举后缓存，滚动容器查询限制为 `Group`。
- 实测精确跳转从约 1.46 秒降至约 0.105 秒，操作位于后台线程。
- 轻量对话探测约 0.57 秒；缓存命中时不执行全量扫描。
- 空闲 8 秒采样 CPU 为 0%，私有内存约 99 MiB，缓存数据约 20 KiB。

## OCR 结论

无障碍接口已成功暴露消息正文和可编程滚动容器，因此没有实现或启用 OCR。`config.json` 中保留 `ocr_fallback_enabled: false`，当前版本不会截图或识别屏幕。
