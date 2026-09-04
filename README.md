# PicoClaw

PicoClaw 是一个用于学习和实践的本地 Coding Agent Runtime。

项目不直接拼接 Pico 与 Learn-OpenClaw 的源码，而是重新实现一条清晰、可测试的最小链路：

```text
用户请求 -> 模型决策 -> 结构化 ToolCall -> Runtime 校验并执行 -> ToolResult 回传 -> 最终回答
```

## 当前阶段：M6 预算化 Goal Loop、Checkpoint、Verifier 与 Benchmark

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
- 使用本地Tokenizer构建输入Token预算
- 压缩旧历史，同时保持工具申请与工具结果不可拆分
- 保护当前用户请求不被截断
- 支持Working、Episodic、Durable三层记忆
- 使用文件SHA-256使过期文件记忆和依赖结论失效
- 启动时只扫描 Skill 的名称和描述，任务命中后才加载正文
- 为 Skill 设置独立 Token 预算，并明确其不能绕过 Runtime 安全策略
- 使用官方 MCP Python SDK 接入 stdio Server
- MCP 工具只有进入显式 allowlist 后才会注册，并使用命名空间避免重名
- MCP 参数仍经过 Schema 校验，调用仍经过风险分级、审批和 Trace
- MCP 连接通过异步上下文管理器开启和关闭，不复用失效 Session
- 使用 Goal Loop 根据独立验收结果组织多次 Agent 尝试
- 同时限制尝试次数、模型步数、工具调用、Token、估算费用、耗时和无进展轮数
- 将 TaskState 原子保存为 Checkpoint，支持中断后继续
- 使用 Runtime Identity 与工作区 SHA-256 Manifest 拒绝过期 Checkpoint
- 使用 File、Command 和 Composite Verifier 检查真实工作区
- 使用隔离 Fixture 和统一指标运行可重复 Benchmark

## 为什么从离线模型开始

第一阶段不接真实大模型，是为了把 Runtime 的正确性和模型输出质量分开验证。只要离线测试通过，就能证明 Agent Loop、工具回传和路径边界本身工作正常；后续接入 OpenAI、Anthropic 或 Ollama 时，出现问题可以定位到 Provider 层。

## 本地验证

```powershell
uv sync
uv run pytest -q
uv run ruff check src tests
uv run picoclaw-demo
uv run picoclaw-m3-demo
uv run picoclaw-m4-demo
uv run picoclaw-m5-demo
uv run picoclaw-m6-demo
```

M4离线演示会展示“首次读取、记忆命中、文件变化后重新读取”：

```powershell
uv run picoclaw-m4-demo
```

M5离线演示会真实启动本地 MCP stdio Server，并展示 Skill 按需加载、MCP 工具注册和受控调用：

```powershell
uv run picoclaw-m5-demo
```

M6离线演示会展示“第一次写错并耗尽预算、从 Checkpoint 恢复、根据 Verifier 反馈修正、Benchmark 拒绝空口成功”：

```powershell
uv run picoclaw-m6-demo
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

真实模型入口默认扫描工作区的 `skills/*/SKILL.md`，也可指定其他工作区内目录：

```powershell
uv run picoclaw-real --skills-dir examples/skills "总结这个代码仓库"
```

通过文件内容验收启用真实 Goal Loop：

```powershell
uv run picoclaw-real `
  --approval ask `
  --verify-file app.cfg `
  --verify-contains "mode=prod" `
  --goal-attempts 3 `
  "创建 app.cfg，并设置 mode=prod"
```

任务中断后，使用完全相同的请求和 Runtime 配置恢复：

```powershell
uv run picoclaw-real --resume --verify-file app.cfg --verify-contains "mode=prod" "创建 app.cfg，并设置 mode=prod"
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
| M4（已完成） | Token 感知上下文、三层记忆、文件哈希失效 |
| M5（已完成） | 渐进式 Skills 与受控 MCP Adapter |
| M6（已完成） | 有预算的 Goal Loop、Checkpoint、Verifier 与 Benchmark |

详细设计见 [docs/architecture.md](docs/architecture.md)。

简历证据边界、推荐项目描述和高频追问见 [docs/interview-evidence.md](docs/interview-evidence.md)。本地发布检查记录见 [docs/validation-report.md](docs/validation-report.md)。
