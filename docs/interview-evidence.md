# PicoClaw 简历与面试证据包

## 1. Fit Verdict

**strong fit（Coding Agent / LLM 应用工程 / AI Backend 实习）**。

项目拥有可以现场运行的 Agent Loop、Provider、工具安全、上下文、记忆、Skills、MCP、Checkpoint、Verifier 和 Benchmark 代码，适合证明 Agent Runtime 工程能力。它没有模型训练、线上流量或真实用户证据，因此不应包装成大模型训练项目或生产系统。

## 2. 可复核证据

| 证据 | 当前结果 | 复核方式 |
| --- | --- | --- |
| 自动化测试 | 本地 48 个测试通过 | `uv run pytest -q` |
| 静态检查 | Ruff check/format 通过 | `uv run ruff check src tests` |
| 包构建 | sdist 与 Wheel 可构建 | `uv build` |
| M4 记忆实验 | 相同文件问题的工具调用从 1 次变为 0 次；文件变化后重新读取 | `uv run picoclaw-m4-demo` |
| M5 MCP 实验 | 真实 stdio Server 被发现、allowlist 注册和调用 | `uv run picoclaw-m5-demo` |
| M6 恢复实验 | 第一次写错并停止，Checkpoint 恢复后第 2 次修正通过 | `uv run picoclaw-m6-demo` |
| Benchmark 真实性 | 模型空口声称成功的案例被拒绝，演示套件通过率为 50% | 查看 M6 benchmark report |

这里的数字只描述固定离线实验和本地测试，不代表线上成功率或生产性能。

## 3. Evidence Contract

| Resume Claim | Level | Evidence | Status | Safer Wording | Interview Proof |
| --- | --- | --- | --- | --- | --- |
| 设计 Coding Agent Harness | C2 | `agent.py`、架构文档、端到端测试 | 可以写 | 设计并实现本地 Coding Agent Runtime | 现场画 Agent Loop 并运行 M1 Demo |
| 支持安全工具执行 | C2 | 风险等级、审批、路径边界、原子写和命令白名单测试 | 可以写 | 构建工具注册、参数校验、风险审批与工作区边界 | 演示 `never` 拒绝写入和越界测试 |
| 降低重复文件读取 | C2 | M4 固定场景中工具调用 1→0，SHA 变化后重新读取 | 谨慎写 | 在离线场景中通过三层记忆将重复读取由 1 次降至 0 次 | 运行 M4 Demo，解释哈希失效 |
| 支持 MCP 工具生态 | C2 | 官方 SDK、真实 stdio Server、allowlist、命名空间和审批测试 | 可以写 | 实现受控 MCP stdio Adapter | 运行 M5 Demo 并指出 Trace |
| 支持长任务可靠恢复 | C2 | TaskState、原子 Checkpoint、Runtime Identity、Manifest 测试 | 可以写 | 实现带身份和文件哈希校验的 Checkpoint 恢复 | 修改文件后演示 stale rejection |
| Agent 自动完成真实开发任务 | C3 | 当前只有确定性离线 Demo，无公开任务集结果 | 补证据后写 | 在限定离线任务中完成工具调用与外部验收闭环 | 增加 SWE-bench 子集或自建公开任务集 |
| 已上线/企业级/高并发 | C3 | 无部署、流量、SLA 或用户记录 | 不能写 | 本地可运行原型 / 个人项目 | 如实说明边界和后续工程计划 |

## 4. 推荐简历版本

**PicoClaw：面向本地代码仓库的安全 Coding Agent Runtime**

个人项目｜Python / OpenAI-compatible API / Ollama / MCP / tiktoken / pytest

- 系统设计：从零实现 Provider 无关的 Agent Loop，将模型输出统一为 `ModelResponse / ToolCall / ToolResult`，支持 OpenAI-compatible 原生 Function Calling 与 Ollama 文本协议回退。
- 安全执行：构建集中式 ToolRegistry，对文件读写和开发命令执行实施 JSON Schema 参数校验、工作区路径边界、原子写入、命令白名单及 `ask/auto/never` 审批，并持久化 Trace 与 Run Report。
- 上下文与扩展：实现 Token 感知的历史压缩和 Working/Episodic/Durable 三层记忆；在固定离线场景中将相同文件问题的重复读取由 1 次降至 0 次，并通过 SHA-256 在文件变化后使旧记忆失效；支持 Skill 按需加载与 allowlist MCP stdio Adapter。
- 长任务可靠性：实现受尝试次数、模型步数、工具、Token、估算费用、时间和无进展轮数约束的 Goal Loop；通过 Runtime Identity、工作区文件 Manifest、独立 Verifier 和隔离 Benchmark 建立“失败—Checkpoint—恢复—外部验收”闭环，项目本地 48 个测试通过。

## 5. 高风险面试问题与回答卡

### 问题一：为什么最终答案不能代表任务成功？

- 危险回答：因为大模型不可靠，所以我又问了它一次。
- 合格回答：模型只负责提出动作和候选答案；成功状态由 Runtime 外部的 Verifier 直接检查真实工作区产生。
- 强回答：M6 演示中模型写入 `mode=dev` 后仍声称完成，File Verifier 读取实际文件并拒绝；失败详情进入下一轮，恢复后修正为 `mode=prod` 才标记成功。

### 问题二：Checkpoint 为什么不能直接反序列化继续？

- 危险回答：JSON 文件存在就可以恢复。
- 合格回答：文件可能被外部修改，工具或模型也可能变化，旧状态可能不再成立。
- 强回答：Checkpoint 同时绑定 Provider/模型、工具 Schema/风险/审批、Verifier、Runtime 源码摘要和工作区 SHA-256 Manifest；任一变化都触发 stale rejection。

### 问题三：为什么说重复读取从 1 次降到 0 次，而不是提升了 100%？

- 危险回答：我的记忆系统把性能提升了 100%。
- 合格回答：这是一个确定性离线样例，不是整体性能指标，因此只描述调用次数变化。
- 强回答：首次任务读取 `config.txt` 一次，第二次从 Episodic Memory 命中而不调用工具；文件 SHA 改变后旧记忆失效并重新读取。要写总体提升比例，需要固定任务集和 baseline 多次评测。

### 问题四：MCP 接入后最危险的地方是什么？

- 危险回答：使用官方 SDK，所以天然安全。
- 合格回答：MCP Server 是外部信任边界，广播的工具不能全部直接暴露给模型。
- 强回答：配置必须由操作员提供，只注册 allowlist 工具，名称加 Server 命名空间，参数重新进入 ToolRegistry 校验，默认风险为 execute，并限制结果长度和多媒体 Base64 注入。

### 问题五：这个项目最大的不足是什么？

- 合格回答：目前主要是本地确定性测试和小型 Demo，没有真实代码任务集、并发 Session、生产部署或性能数据。
- 改进方向：增加公开 Benchmark 子集、多次采样统计、持久 MCP Session、结构化事件 Schema、配置文件与敏感字段审计，再根据真实结果决定能否写成功率或性能提升。

## 6. 30 秒项目介绍

PicoClaw 是我从零实现的本地 Coding Agent Runtime。模型只负责分析和产生工具调用，Runtime 负责上下文、工具校验、安全审批、记忆和审计。项目支持 OpenAI-compatible、Ollama、渐进式 Skills 和受控 MCP；针对长任务又加入多维预算、Checkpoint 和独立 Verifier，不以模型最终回答作为成功标准，而是检查真实工作区。当前用 48 个自动化测试以及 M4–M6 离线演示验证核心链路，定位是可学习、可解释的本地原型，不包装成生产系统。
