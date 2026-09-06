# Changelog

本项目遵循语义化版本思路记录重要变化。

## 0.1.0 - 2026-09-06

### Added

- Provider 无关的 Agent Loop 与统一 ToolCall/ToolResult 协议。
- OpenAI-compatible 原生 Function Calling 与 Ollama 文本协议回退。
- 工作区路径边界、工具参数校验、风险等级和 ask/auto/never 审批。
- 原子文件写入、开发命令白名单、Trace 与 Run Report。
- Token 感知上下文压缩与 Working/Episodic/Durable 三层记忆。
- 基于文件 SHA-256 的记忆失效。
- 渐进式 Skills 与受控 MCP stdio Adapter。
- 多维预算 Goal Loop、可信 Checkpoint、独立 Verifier 与隔离 Benchmark。
- M1–M6 离线演示、48 个自动化测试和 Python 3.12/3.13 CI 配置。

### Known limitations

- 当前定位为本地学习原型，没有生产部署、真实用户或 SLA 数据。
- MCP 每次调用重新建立连接，高频工具调用会有额外启动开销。
- Benchmark 以确定性本地案例为主，尚未接入公开 Coding Agent 任务集。
- Checkpoint Manifest 默认最多扫描 5000 个非运行工件文件，并跳过符号链接。
