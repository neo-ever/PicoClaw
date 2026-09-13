# PicoClaw 架构决策

## 1. 项目目标

PicoClaw 的目标不是再造一个聊天机器人，而是实现一个让模型能够在本地代码仓库中持续、受控、可验证地工作的 Agent Runtime。

模型只负责提出决策，Runtime 掌握实际执行权。

## 2. 从两个项目吸收什么

| 来源 | 吸收能力 | 暂不直接复制的部分 |
| --- | --- | --- |
| Pico | 工作区边界、安全审批、状态、Trace、Checkpoint、Verifier、消融评测 | XML 工具协议将仅作为不支持 Function Calling 时的回退方案 |
| Learn-OpenClaw | 原生 `tool_calls`、工具消息成组、Token Usage、Skills、MCP、Goal Loop 思想 | 无边界 Bash、教学版 MCP 生命周期、无限 Goal Loop |

## 3. M2 数据流

```text
User Message
    |
    v
AgentLoop ------ tools schema ------> ModelProvider
    ^                               /                 \
    |                  OpenAI native tool_calls    Ollama text tags
    |                              \                 /
ToolResult <--- ToolRegistry <------- canonical ModelResponse
                    |
                    v
                Workspace
```

## 4. 四条架构规则

1. **Provider 只负责翻译协议**：不同模型输出都转换成统一 `ModelResponse`。
2. **模型没有执行权**：模型只能产生 `ToolCall`，Runtime 决定是否执行。
3. **工具集中注册**：工具描述、参数规则和执行函数不能散落在 Prompt 中。
4. **先可测试，再智能化**：先用确定性模型验证 Runtime，再接真实模型。

## 5. Provider 边界

- `OpenAICompatibleProvider` 负责 Chat Completions 消息转换、原生工具调用解析和 SDK 错误归一化。
- `OllamaTextProvider` 负责生成带工具 Schema 的 Prompt，并解析 `<tool>` 或 `<final>` 标签。
- 两条路径都只能返回 PicoClaw 自己的 `ModelResponse`，Agent Loop 不导入任何厂商响应类型。
- `ProviderConfig` 从环境变量加载密钥，配置对象的 `repr` 不显示 API Key。
- API 返回的 Token Usage 会跨 Agent Loop 的多轮调用累加。

## 6. 后续必须保持的边界

- MCP 工具也必须进入统一 `ToolRegistry`，不能绕开安全检查。
- Skill 只提供说明和受控资源，不能天然获得系统权限。
- Goal Loop 必须有步数、时间、Token/费用和无进展停止条件。
- Memory 中的文件事实必须绑定内容哈希，文件变化后需要失效或重新读取。

## 7. M6 Goal Loop 数据流

```text
GoalRunner -> Agent.run(remaining budget) -> candidate answer
    |                                      |
    |                                      v
    +---- retry feedback <- Verifier <- real Workspace
    |
    +---- TaskState + RuntimeIdentity + WorkspaceManifest -> Checkpoint
    |
    +---- isolated fixtures -> Benchmark report
```

最终成功状态只能由 Verifier 产生，模型回答本身不能将 Goal 标记为成功。

## 8. M7 Context Selection 与数据闭环

```text
Workspace -> RepositoryChunker -> Lexical candidate pool
                                      |
                 +--------------------+--------------------+
                 |                    |                    |
             Lexical            HTTP/Linear Delta       Full Context
                 |                    |                    |
                 +---------- ContextSelector -------------+
                                      |
                              Selected Evidence
                                      |
                              ContextManager -> Agent
                                      |
                              external Verifier outcome
                                      |
                     minus/plus Pair + direct delta label
                                      |
                        training -> /predict_delta_batch
```

架构约束：

1. Selector 只有建议权，没有文件或命令工具权限。
2. 候选代码块必须先由 Workspace 边界内的 Chunker 产生，远端不能返回任意路径。
3. Evidence、Memory 和 History 使用独立语义与预算。
4. HTTP 失败必须降级到本地策略，不能阻断 Agent Loop。
5. 训练与在线服务共同使用 Direct Verifier Delta Prompt，不再混用答案 Logprob 与问题 Logprob。
6. 数据按任务分组切分，策略对照在隔离工作区运行，并由相同外部 Verifier 判定。
