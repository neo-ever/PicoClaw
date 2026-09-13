# PicoClaw 简历与面试证据包

## 1. Fit Verdict

**strong fit（Coding Agent / LLM 应用工程 / AI Backend 实习）**。

项目拥有可以现场运行的 Agent Loop、Provider、工具安全、上下文、记忆、Skills、MCP、Checkpoint、Verifier 和 Context Selection Benchmark，适合证明 Agent Runtime 工程能力。它完成了合成数据上的 Linear Delta 训练闭环和 Qwen 训练入口，但没有真实 0.8B 微调、线上流量或真实用户证据，因此不应包装成已验证的大模型训练项目或生产系统。

## 2. 可复核证据

| 证据 | 当前结果 | 复核方式 |
| --- | --- | --- |
| 自动化测试 | 本地 69 个测试通过 | `uv run pytest -q` |
| 静态检查 | Ruff check/format 通过 | `uv run ruff check src tests` |
| 包构建 | sdist 与 Wheel 可构建 | `uv build` |
| M4 记忆实验 | 相同文件问题的工具调用从 1 次变为 0 次；文件变化后重新读取 | `uv run picoclaw-m4-demo` |
| M5 MCP 实验 | 真实 stdio Server 被发现、allowlist 注册和调用 | `uv run picoclaw-m5-demo` |
| M6 恢复实验 | 第一次写错并停止，Checkpoint 恢复后第 2 次修正通过 | `uv run picoclaw-m6-demo` |
| Benchmark 真实性 | 模型空口声称成功的案例被拒绝，演示套件通过率为 50% | 查看 M6 benchmark report |
| M7-C 数据闭环 | 18/6 条合成 Train/Validation，任务交集为 0 | `uv run picoclaw-m7c-demo` |
| M7-C 策略对照 | 5 策略 × 3 隔离案例均经外部 File Verifier 验收 | 查看 Context Benchmark report |

这里的数字只描述固定离线实验和本地测试，不代表线上成功率或生产性能。

## 3. Evidence Contract

| Resume Claim | Level | Evidence | Status | Safer Wording | Interview Proof |
| --- | --- | --- | --- | --- | --- |
| 设计 Coding Agent Harness | C2 | `agent.py`、架构文档、端到端测试 | 可以写 | 设计并实现本地 Coding Agent Runtime | 现场画 Agent Loop 并运行 M1 Demo |
| 支持安全工具执行 | C2 | 风险等级、审批、路径边界、原子写和命令白名单测试 | 可以写 | 构建工具注册、参数校验、风险审批与工作区边界 | 演示 `never` 拒绝写入和越界测试 |
| 降低重复文件读取 | C2 | M4 固定场景中工具调用 1→0，SHA 变化后重新读取 | 谨慎写 | 在离线场景中通过三层记忆将重复读取由 1 次降至 0 次 | 运行 M4 Demo，解释哈希失效 |
| 支持 MCP 工具生态 | C2 | 官方 SDK、真实 stdio Server、allowlist、命名空间和审批测试 | 可以写 | 实现受控 MCP stdio Adapter | 运行 M5 Demo 并指出 Trace |
| 支持长任务可靠恢复 | C2 | TaskState、原子 Checkpoint、Runtime Identity、Manifest 测试 | 可以写 | 实现带身份和文件哈希校验的 Checkpoint 恢复 | 修改文件后演示 stale rejection |
| 支持自适应代码上下文 | C2 | 安全切块、Lexical/HTTP/Linear/Full 策略、Token 预算、Trace 与 Context Benchmark | 可以写 | 构建可替换 Context Selector 与隔离 A/B 评测框架 | 运行 M7-C Demo，比较文件读取和 Prompt Token |
| 已完成 Qwen 0.8B 代码微调 | C3 | 当前无 GPU、权重和真实训练产物 | 不能写 | 实现 Direct Delta 数据契约及 Qwen 训练/服务入口 | 展示 CLI 与 Schema，并说明待 GPU 运行 |
| Agent 自动完成真实开发任务 | C3 | 当前只有确定性离线 Demo，无公开任务集结果 | 补证据后写 | 在限定离线任务中完成工具调用与外部验收闭环 | 增加 SWE-bench 子集或自建公开任务集 |
| 已上线/企业级/高并发 | C3 | 无部署、流量、SLA 或用户记录 | 不能写 | 本地可运行原型 / 个人项目 | 如实说明边界和后续工程计划 |

## 4. 推荐简历版本

**PicoClaw：面向本地代码仓库的安全 Coding Agent Runtime**

个人项目｜Python / OpenAI-compatible API / Ollama / MCP / tiktoken / pytest

- 系统设计：从零实现 Provider 无关的 Agent Loop，将模型输出统一为 `ModelResponse / ToolCall / ToolResult`，支持 OpenAI-compatible 原生 Function Calling 与 Ollama 文本协议回退。
- 安全执行：构建集中式 ToolRegistry，对文件读写和开发命令执行实施 JSON Schema 参数校验、工作区路径边界、原子写入、命令白名单及 `ask/auto/never` 审批，并持久化 Trace 与 Run Report。
- 上下文与扩展：实现 Token 感知的历史压缩和 Working/Episodic/Durable 三层记忆；在固定离线场景中将相同文件问题的重复读取由 1 次降至 0 次，并通过 SHA-256 在文件变化后使旧记忆失效；支持 Skill 按需加载与 allowlist MCP stdio Adapter。
- 长任务可靠性：实现受尝试次数、模型步数、工具、Token、估算费用、时间和无进展轮数约束的 Goal Loop；通过 Runtime Identity、工作区文件 Manifest、独立 Verifier 和隔离 Benchmark 建立“失败—Checkpoint—恢复—外部验收”闭环。
- 自适应上下文：实现安全代码切块及 Lexical、HTTP Delta、Linear Delta 和 Full Context 策略，通过候选初筛、批量评分、LRU 缓存、超时降级和 Evidence Token 预算减少无关上下文；建立 `picoclaw-delta-v1` Pair 数据与任务级切分，并在 5 策略 × 3 个确定性案例上生成统一 A/B 报告，项目本地 69 个测试通过。

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

### 问题六：为什么 M7-C 的 100% 不能写成模型准确率？

- 危险回答：我的 Selector 准确率达到了 100%。
- 合格回答：这是 6 条刻意可分的合成验证样本，只用于证明数据、训练和评测程序贯通。
- 强回答：我同时报告样本规模、任务级零泄漏和数据来源；真实结论必须在代码领域任务上运行 Qwen 0.8B，并与 no-evidence、BM25 和 full-context 使用固定 Reader、预算及外部 Verifier 对照。

## 6. 30 秒项目介绍

PicoClaw 是我从零实现的本地 Coding Agent Runtime。模型只负责决策，Runtime 负责上下文、工具安全、记忆、审计和外部验收。针对大仓库，我又加入可替换 Context Selector，通过安全切块、词法初筛、批量 Delta 打分、缓存和降级，在 Token 预算内构造 Evidence；同时建立任务级 Pair 数据和多策略隔离 Benchmark。当前有 69 个自动化测试，真实 0.8B 微调仍待 GPU 与代码领域数据，不包装成生产效果。
