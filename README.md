# PicoClaw

PicoClaw 是一个用于学习和实践的本地 Coding Agent Runtime。

项目不直接拼接 Pico 与 Learn-OpenClaw 的源码，而是重新实现一条清晰、可测试的最小链路：

```text
用户请求 -> 模型决策 -> 结构化 ToolCall -> Runtime 校验并执行 -> ToolResult 回传 -> 最终回答
```

## 当前阶段：M3 安全执行与可观测性

- 统一 `ModelResponse`、`ToolCall` 和 `ToolResult`
- 通过 `ModelProvider` 协议隔离具体模型厂商
- 通过 `ToolRegistry` 注册、描述、校验和执行工具
- 通过 `Workspace` 阻止文件工具越出仓库根目录
- 通过 `AgentLoop` 运行有最大步数限制的决策循环
- 使用确定性 `ScriptedProvider` 做离线端到端测试
- 支持 OpenAI-compatible Chat Completions 原生 Function Calling
- 支持 Ollama `<tool>/<final>` 文本协议回退
- 从环境变量加载模型配置，并在日志表示中隐藏 API Key
- 将模型错误归一为认证、连接、限流、请求和协议错误
- 跨模型统一累计 Prompt、Completion 和 Total Token Usage
- 为工具标注 `read_only`、`write`、`execute` 风险等级
- 支持 `ask`、`auto`、`never` 三种审批策略
- 支持工作区内原子 `write_file`
- 支持无 Shell 解释器的白名单 `run_shell`
- 将 Trace 追加写入 JSONL，并原子生成最终 Report

## 为什么从离线模型开始

第一阶段不接真实大模型，是为了把 Runtime 的正确性和模型输出质量分开验证。只要离线测试通过，就能证明 Agent Loop、工具回传和路径边界本身工作正常；后续接入 OpenAI、Anthropic 或 Ollama 时，出现问题可以定位到 Provider 层。

## 本地验证

```powershell
uv sync
uv run pytest -q
uv run ruff check src tests
uv run picoclaw-demo
uv run picoclaw-m3-demo
```

M3交互演示会依次申请写文件和运行白名单命令。输入`y`批准，直接回车拒绝：

```powershell
uv run picoclaw-m3-demo --approval ask
```

运行工件保存在：

```text
.picoclaw/m3-demo-workspace/.picoclaw/runs/<run_id>/
├── trace.jsonl
└── report.json
```

## OpenAI-compatible 真实模型

先在当前 PowerShell 设置环境变量：

```powershell
$env:PICOCLAW_PROVIDER="openai"
$env:PICOCLAW_MODEL="你的模型ID"
$env:PICOCLAW_API_KEY="你的API Key"
$env:PICOCLAW_BASE_URL="https://你的兼容接口/v1"
uv run picoclaw-real
```

API Key 只存在于当前进程环境，不要写入源码、README 或 Git。

## 本地 Ollama 文本回退

```powershell
$env:PICOCLAW_PROVIDER="ollama"
$env:PICOCLAW_OLLAMA_MODEL="你的本地模型名"
uv run picoclaw-real --provider ollama
```

真实模型入口默认使用`--approval ask`。可显式选择：

```powershell
uv run picoclaw-real --approval never
uv run picoclaw-real --approval ask
uv run picoclaw-real --approval auto
```

`auto`仅建议在可丢弃、已隔离的测试工作区使用。

Ollama 模型被要求返回下面两种格式之一：

```text
<tool>{"name":"read_file","arguments":{"path":"README.md"}}</tool>
<final>最终答案</final>
```

这条路径用于不支持原生 Function Calling 的本地模型。格式错误会被归类为 `ProviderProtocolError`，而不会让未验证文本进入工具执行层。

## 迭代路线

| 里程碑 | 能力 |
| --- | --- |
| M1 | 最小 Agent Loop、统一工具协议、只读工作区工具 |
| M2（已完成） | OpenAI-compatible 原生 Function Calling 与 Ollama 文本协议回退 |
| M3（已完成） | 工具风险等级、审批、写文件、Shell、Trace 与 Run Report |
| M4 | Token 感知上下文、三层记忆、文件哈希失效 |
| M5 | 渐进式 Skills 与受控 MCP Adapter |
| M6 | 有预算的 Goal Loop、Checkpoint、Verifier 与 Benchmark |

详细设计见 [docs/architecture.md](docs/architecture.md)。
