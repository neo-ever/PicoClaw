# PicoClaw

[![CI](https://github.com/neo-ever/PicoClaw/actions/workflows/ci.yml/badge.svg)](https://github.com/neo-ever/PicoClaw/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

PicoClaw 是一个用于学习和实践的本地 Coding Agent Runtime。

项目不直接拼接 Pico 与 Learn-OpenClaw 的源码，而是重新实现一条清晰、可测试的最小链路：

```text
用户请求 -> 模型决策 -> 结构化 ToolCall -> Runtime 校验并执行 -> ToolResult 回传 -> 最终回答
```

## 快速开始

环境要求：Python 3.12+ 与 [uv](https://docs.astral.sh/uv/)。

```powershell
git clone https://github.com/neo-ever/PicoClaw.git
cd PicoClaw
uv sync --locked --dev
uv run pytest -q
uv run picoclaw-demo
```

不配置 API Key 也可以运行 M1、M3、M4、M5、M6 和 M7-A 的确定性离线演示。真实模型配置见下文。

## 项目定位

| 是什么 | 不是什么 |
| --- | --- |
| 用于学习 Agent Runtime 关键机制的本地可运行实现 | 已上线的生产 Coding Agent |
| 具有工具边界、审批、记忆、MCP、恢复与验收的原型 | 拥有真实用户规模或线上 SLA 的商业系统 |
| 通过固定离线实验和自动化测试验证的工程项目 | 大模型训练或效果提升百分比实验 |

## 当前阶段：M7-C 数据、训练与 A/B 评测闭环

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
- 在 Workspace 边界内按文件和行号切分代码，跳过缓存、构建目录、符号链接和常见敏感文件
- 使用本地 BM25 风格相关度与重复惩罚，在独立 Token 预算内选择仓库证据
- 将 Selected Evidence 作为不可信数据注入 ContextManager，不赋予 Selector 任何工具权限
- 在 Trace 与 Report 中记录候选数、代码块、来源 SHA-256、分数、Token 和耗时
- 选择器不可用时记录失败并降级，不阻断原有 Agent Loop
- 通过 `/score_batch` 接入 Qwen 0.8B Selector，使用边际增益贪心选择代码块
- 先用词法相关度缩小候选池，再分批发送，避免对整个仓库逐块调用模型
- 使用有界内存 LRU 缓存复用 `(question, context)` 分数，缓存键不保留源码正文
- HTTP 超时、连接失败、非法 JSON、数量不匹配或非有限分数都会自动回退词法选择
- 默认只允许回环地址；向远程 Selector 发送仓库代码必须显式开启授权
- Selector 类型、URL、批大小、候选池和切块配置进入 Runtime Identity，配置变化会使旧 Checkpoint 失效
- 使用 `picoclaw-delta-v1` 统一训练与在线推理的直接 Verifier Delta 契约
- 按任务 ID 分组切分代码领域 Pair 数据，阻止同一任务泄漏到训练集和验证集
- 提供无需 PyTorch 的 Linear Delta 可训练基线，以及 Qwen 0.8B Regression 训练和批量服务入口
- 使用隔离工作区和外部 Verifier 对 no-evidence、lexical、HTTP、full-context 和 learned 策略运行统一 A/B Benchmark

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
uv run picoclaw-m7-demo
uv run picoclaw-m7b-demo
uv run picoclaw-m7c-demo
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

M7-A离线演示会扫描一个小型代码仓库，在 Token 预算内选中认证代码，并在不调用读取工具的情况下把证据交给模型：

```powershell
uv run picoclaw-m7-demo
```

M7-B 离线演示使用模拟的 `/score_batch` 服务契约，展示批量打分、第二次请求缓存命中和超时词法降级：

```powershell
uv run picoclaw-m7b-demo
```

M7-C 离线演示会构造按任务隔离的 Delta 数据、训练并验证轻量基线，再运行多策略外部 Verifier Benchmark：

```powershell
uv run picoclaw-m7c-demo
```

把训练迁移到 GPU 机器前，可用预检命令一次检查依赖、CUDA、模型位置、数据 Schema、任务泄漏和标签分布：

```powershell
uv run picoclaw-qwen-preflight `
  --model "你的Qwen模型目录或模型ID" `
  --train .picoclaw/delta-data/train.jsonl `
  --validation .picoclaw/delta-data/validation.jsonl
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

使用内置词法 Selector 预先选择仓库证据：

```powershell
uv run picoclaw-real --evidence --evidence-tokens 1200 "认证逻辑在哪里实现？"
```

`--evidence` 默认关闭，因此不会改变原有运行方式。M7-A 只使用本地词法基线，不会把仓库内容发送给额外的远程服务。

接入运行在本机 `6006` 端口的 0.8B Selector：

```powershell
$env:PICOCLAW_SELECTOR_URL="http://127.0.0.1:6006"
uv run picoclaw-real `
  --evidence `
  --evidence-selector http `
  --selector-timeout 8 `
  --selector-batch-size 8 `
  --selector-candidate-pool 24 `
  "认证逻辑在哪里实现？"
```

如果 Selector 位于其他主机，必须额外传入 `--allow-remote-selector`。这表示你明确同意把初筛后的仓库代码发送到该地址。

使用与训练目标对齐的 Direct Delta 服务：

```powershell
uv run picoclaw-real `
  --evidence `
  --evidence-selector http-direct `
  --selector-url http://127.0.0.1:6007 `
  "认证逻辑在哪里实现？"
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
| M7-A（已完成） | 安全代码切块、词法选择基线、Evidence 注入与选择审计 |
| M7-B（已完成） | 0.8B Selector HTTP Adapter、批量打分、LRU 缓存、超时与词法降级 |
| M7-C1（已完成） | 多策略隔离 Context Benchmark 和统一成本/质量指标 |
| M7-C2（已完成） | Direct Verifier Delta 训练—推理契约与批量 HTTP Adapter |
| M7-C3（已完成） | 代码 Pair 数据构建、Schema 校验、JSONL 和按任务分组切分 |
| M7-C4（部分完成） | Linear Delta 已训练实跑；Qwen 0.8B 训练/服务入口完成，待 GPU 与权重运行 |
| M7-C5（已完成） | 离线多策略 A/B、外部 Verifier 和统一 JSON 报告 |

详细设计见 [docs/architecture.md](docs/architecture.md)。

简历证据边界、推荐项目描述和高频追问见 [docs/interview-evidence.md](docs/interview-evidence.md)。本地发布检查记录见 [docs/validation-report.md](docs/validation-report.md)。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| [架构决策](docs/architecture.md) | 总体边界和 M1–M7 数据流 |
| [M4 上下文与记忆](docs/m4-context-and-memory.md) | Token 压缩、三层记忆、SHA 失效 |
| [M5 Skills 与 MCP](docs/m5-skills-and-mcp.md) | 渐进式加载和受控外部工具 |
| [M6 Goal Loop](docs/m6-goal-loop.md) | 预算、Checkpoint、Verifier、Benchmark |
| [M7-A 上下文选择](docs/m7-context-selection.md) | 代码切块、词法选择、Evidence 注入和代码导读 |
| [M7-B HTTP Selector](docs/m7b-http-selector.md) | 0.8B 服务契约、贪心 Delta、缓存、超时和降级 |
| [M7-C 数据与评测闭环](docs/m7c-data-training-benchmark.md) | Direct Delta 数据、训练入口、多策略 Benchmark 与结果边界 |
| [M7-C GPU 训练交接](docs/m7c-gpu-handoff.md) | 可选依赖、训练预检、GPU 微调、服务和最终验收步骤 |
| [5 分钟演示脚本](docs/demo-script.md) | 面试或项目展示时的操作顺序 |
| [简历与面试证据](docs/interview-evidence.md) | 事实边界、推荐措辞和追问答案 |
| [本地验证记录](docs/validation-report.md) | 测试、构建和隔离安装结果 |

贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全问题与密钥处理见 [SECURITY.md](SECURITY.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。
