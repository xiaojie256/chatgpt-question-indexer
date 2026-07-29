# Security Policy

## Supported versions

目前仅维护最新的 `main` 分支和最新 GitHub Release。

## Reporting a vulnerability

请不要公开提交包含漏洞利用细节、真实对话、Token、Cookie 或本地路径的 Issue。

优先使用 GitHub 仓库的 **Private vulnerability reporting**。如果该功能不可用，请通过仓库所有者的 GitHub 个人资料联系维护者，并提供：

- 受影响版本；
- 可复现步骤；
- 预期影响；
- 已脱敏的日志或最小示例。

维护者会尽快确认报告并协调修复与披露时间。

## Security boundaries

本项目只使用 Windows UI Automation 和公开的窗口 API。它不应：

- 访问 OpenAI API；
- 读取账号 Cookie、Token 或凭据；
- 读取 ChatGPT/Codex 私有数据库；
- 修改桌面应用安装文件；
- 在运行时发送网络请求。

本地索引包含完整用户提问，应按照敏感本地数据保护。
