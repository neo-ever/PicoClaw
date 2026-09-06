# Contributing

## 开发环境

```powershell
uv sync --locked --dev
```

## 提交前检查

```powershell
uv run ruff format src tests
uv run ruff check src tests
uv run pytest -q
uv build
```

## 设计边界

- Provider 只负责模型协议转换，不直接执行工具。
- 模型只能申请 ToolCall，实际执行必须经过 ToolRegistry。
- 文件访问必须经过 Workspace 路径边界。
- 新增危险工具时必须声明风险等级并补充审批测试。
- Skill 只能提供任务指导，不能改变 Runtime 权限。
- MCP 工具必须经过显式 allowlist、参数校验和风险审批。
- Goal 成功只能由独立 Verifier 产生，不能相信模型的自我声明。
- 强结果声明必须同时提交指标定义、基线和可复核报告。

## Pull Request 建议

请说明修改目标、设计取舍、风险边界、验证命令和结果。涉及行为变化时应增加自动化测试与对应文档。
