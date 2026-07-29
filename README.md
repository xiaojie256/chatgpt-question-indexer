# ChatGPT Question Indexer

[![CI](https://github.com/xiaojie256/chatgpt-question-indexer/actions/workflows/ci.yml/badge.svg)](https://github.com/xiaojie256/chatgpt-question-indexer/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D4.svg)](https://www.microsoft.com/windows/)

Privacy-first local question navigation sidebar for the ChatGPT/Codex Windows desktop app, powered by Microsoft UI Automation.

一个本地优先、无需浏览器和 OpenAI API 的 ChatGPT/Codex Windows 桌面端提问导航侧边栏。

> [!IMPORTANT]
> 本项目是独立的社区开源项目，与 OpenAI 无隶属、授权或背书关系。“ChatGPT”和“Codex”是其各自权利人的商标。

这是一个仅在本机运行的 Windows 侧边栏。它通过 Microsoft UI Automation 读取当前 ChatGPT 桌面应用的可访问树，为当前对话中的用户提问建立目录。

本项目不使用浏览器、OpenAI API、Cookie、Token、应用私有数据库或固定安装路径，不修改 ChatGPT。运行时不发起网络请求。日志只记录消息哈希、长度和错误，不记录完整对话文本。

## 当前实测环境

- Windows 10 Pro for Workstations，版本 10.0.19045
- 新版 ChatGPT/Codex 桌面应用，窗口标题 `ChatGPT`
- Python 3.13

macOS 代码未实现，因为要求只实现自动检测到的当前系统。

## 安装与运行

先启动 ChatGPT 桌面应用并打开一个对话，然后在 PowerShell 5.1 中运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\start.ps1
```

脚本会创建 `.venv`、安装依赖并启动侧边栏。依赖安装需要联网一次；应用自身运行时不联网。也可以直接运行已经打包的 exe。

Windows 辅助功能接口通常不需要单独授权。如果 ChatGPT 以管理员身份运行，本工具也应以管理员身份运行；反之两者都应普通运行。检测到权限级别不同时，程序会显示提示。

## 使用

- 侧边栏默认宽约 280 像素并始终置顶。
- 启动时只贴靠一次，并为侧边栏留出空间。之后不会定时修改 ChatGPT/Codex 的位置或尺寸。
- 用户改变 Codex 尺寸后，侧栏保持原位；需要重新排列时点击 `↔`，或从托盘选择“重新贴靠一次”。
- 侧栏是普通 Windows 顶层窗口，仅属于启动 exe 时所在的虚拟桌面，不会主动跨虚拟桌面跟随。
- 点击 `◀` 折叠，拖动窗口左边缘调整宽度。
- 点击提问条目跳到对应消息，悬停查看完整正文。
- 连续快速点击时仅执行最后一次待处理跳转；临时高亮会自动恢复。
- `重建索引` 会从顶部扫描到底部，并恢复扫描前位置。
- `清除数据` 删除 `data/indexes` 下的全部本地索引。
- 关闭窗口会最小化到托盘；在托盘菜单选择 `退出` 才会完全退出。

默认全局快捷键：

- `Ctrl+Shift+I`：显示/隐藏目录
- `Ctrl+Shift+R`：重建索引

可在 `config.json` 中修改快捷键、宽度和自动刷新间隔。快捷键冲突时状态栏会提示。

## 索引缓存与资源限制

- 当前对话只在内存中保留一份完整索引；完整正文悬停提示按需读取，不在 Qt 列表项中复制。
- 索引写入 `data\indexes`，并通过小型 `catalog.json` 按窗口标题和对话标题快速定位。
- 启动和切换对话时先做轻量探测；命中缓存后直接显示，只合并当前新消息。仅缓存未命中或手动点击“重建”时执行全量滚动扫描。
- 默认最多保留 30 个对话、合计 25 MiB、最长 90 天。超过任一限制时优先清理最久未访问的索引。
- 这些限制可通过 `cache_max_conversations`、`cache_max_megabytes`、`cache_retention_days` 修改。
- 日志滚动保存：单文件上限 1 MiB，最多两个历史备份。
- 自动事件采用静默期防抖；15 秒低频检查仅作为事件丢失时的保险。

## 诊断

重新生成无障碍树：

```powershell
.\.venv\Scripts\python.exe .\diagnose_accessibility.py
```

输出：

- `diagnostics/accessibility-tree.txt`
- `diagnostics/summary.json`
- 调查结论：`diagnostics/report.md`

## 测试

```powershell
$env:PYTHONPATH = ".\src"
.\.venv\Scripts\python.exe -m pytest -q
```

16 项测试覆盖用户/助手角色区分、中文文本、虚拟化分页去重、相邻特征、50 字摘要、缓存命中、容量清理、过期清理、视口过滤、滚动步长、瞬态节点重试和精确跳转候选选择。

## 打包

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\package.ps1
```

输出位置：

`dist\ChatGPTQuestionIndexer\ChatGPTQuestionIndexer.exe`

PyInstaller 的 onedir 产物必须连同同目录依赖一起分发。

## 故障排查

- 找不到窗口：确认 ChatGPT 桌面应用已启动且主窗口没有处于销毁/重建瞬间，再点重建。
- 没有提问：先运行诊断器，检查 `thread-scroll-container`、`You said:` 和 `ChatGPT said:` 是否仍存在；应用升级可能改变无障碍结构。
- 无法跳转：程序会自动从精确 `ScrollIntoView` 回退到百分比跳转。重新建立索引可刷新失效元素和百分比。
- 改变 Codex 尺寸后侧栏不会跟随，这是为避免拖动卡顿的预期行为；需要时手动点击 `↔`。
- 在其他 Windows 虚拟桌面看不到侧栏是预期行为；请回到启动 exe 时所在的桌面。
- 快捷键无效：在 `config.json` 改成未被占用的组合后重启。
- 权限错误：让 ChatGPT 和本工具使用相同权限级别运行。
- 查看错误：打开 `logs\app.log`。日志不会包含完整对话正文。
- 高 DPI/多显示器：程序不会在显示器变化后自动追赶窗口；请在目标桌面和显示器上点击 `↔` 手动贴靠一次。

## 本地数据

- 配置：`config.json`
- 索引：`data\indexes\*.json`，目录映射为 `data\indexes\catalog.json`
- 日志：`logs\app.log`

索引 JSON 按需求包含完整消息文本，因此应按本机敏感数据保护；它从不上传。

## 项目文档

- [架构与数据流](docs/ARCHITECTURE.md)
- [版本变更](CHANGELOG.md)
- [贡献指南](CONTRIBUTING.md)
- [安全政策](SECURITY.md)

## 参与贡献

欢迎提交缺陷报告、功能建议和拉取请求。开始前请阅读贡献指南；涉及隐私或安全问题时，请按照安全政策私下报告，不要在公开 Issue 中附上对话正文、诊断树、Token 或其他敏感信息。

## 许可证

本项目以 [MIT License](LICENSE) 开源。
