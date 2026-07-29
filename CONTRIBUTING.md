# Contributing

感谢你参与 ChatGPT Question Indexer。

## 开发环境

- Windows 10/11
- Python 3.11+
- PowerShell 5.1+
- 正在运行的 ChatGPT/Codex Windows 桌面应用

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:PYTHONPATH = ".\src"
.\.venv\Scripts\python.exe -m pytest -q
```

## 提交改动

1. 先创建 Issue，描述问题、桌面应用版本和 Windows 版本。
2. 从 `main` 创建功能分支。
3. 保持 UI Automation 访问在后台线程执行。
4. 不得引入浏览器自动化、OpenAI API、Cookie/Token 读取或私有数据库访问。
5. 不得在测试、日志、截图或诊断文件中提交真实对话内容。
6. 为解析、缓存或跳转逻辑补充测试。
7. 提交前运行测试，并说明人工验证范围。

## Pull Request

PR 应包含：

- 改动目的和用户影响；
- 根因与实现方式；
- 隐私和兼容性影响；
- 自动测试与人工测试结果；
- UI 变化截图（必须使用脱敏对话）。

提交即表示你同意按 MIT License 发布贡献。
