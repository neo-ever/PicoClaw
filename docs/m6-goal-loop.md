# M6：预算化 Goal Loop、Checkpoint、Verifier 与 Benchmark

## 1. 为什么普通 Agent Loop 不够

普通 Agent Loop 的结束条件通常是“模型返回了一段最终答案”。这只能证明模型说自己做完了，不能证明仓库真的满足要求。

M6 把完成条件改为：外部 Verifier 检查真实工作区并通过。模型的答案只是一条候选结果，不是验收证据。

## 2. 大白话理解

可以把 M6 想成一个开发团队：

| 组件 | 现实角色 | 职责 |
| --- | --- | --- |
| Agent | 开发人员 | 阅读、修改、运行工具并汇报结果 |
| GoalRunner | 项目经理 | 控制轮次、预算、重试和停止条件 |
| Verifier | 测试/验收人员 | 直接检查文件或运行测试，不听开发人员口头保证 |
| Checkpoint | 工作交接单 | 保存已经做了什么、花了多少预算、上次为何失败 |
| Benchmark | 统一考试 | 在隔离环境中重复执行任务并统计真实通过率 |

## 3. 执行流程

```text
原始目标
  ↓
检查累计预算
  ↓
Agent 执行一次尝试
  ↓
外部 Verifier 检查真实工作区
  ├─ 通过 → 保存成功状态和 Report
  └─ 失败 → 保存 Checkpoint
               ↓
       将失败详情加入下一次 Prompt
               ↓
       继续尝试 / 预算耗尽 / 无进展停止
```

## 4. 七类预算

`GoalBudget` 同时限制：

1. `max_attempts`：最多尝试几轮。
2. `max_model_steps`：累计调用模型多少步。
3. `max_tool_calls`：累计执行多少次工具。
4. `max_total_tokens`：累计消耗多少 Token。
5. `max_cost_usd`：根据显式 Token 单价计算的费用上限。
6. `max_seconds`：累计运行时间。
7. `max_stagnant_attempts`：连续多少轮没有改变工作区就停止。

工具调用预算会在执行工具前检查，因此预算为零时不会触碰工作区。Token 和费用只能在模型接口返回 Usage 后得知，因此可能最多超出一个模型响应的用量，之后立即停止。

## 5. Checkpoint 为什么可信

Checkpoint 不只保存对话摘要，还保存三类东西：

| 内容 | 用途 |
| --- | --- |
| `TaskState` | 尝试次数、Token、费用、反馈、历史结果和停止原因 |
| `RuntimeIdentity` | Provider/模型、工具 Schema/风险/审批、Verifier 配置和 PicoClaw 源码摘要 |
| `WorkspaceManifest` | 排除运行工件后的工作区文件路径与 SHA-256 |

恢复时会重新计算 Runtime Identity 和 Workspace Manifest。模型、工具、验收规则、Runtime 源码或工作区文件发生变化，Checkpoint 都会被判定为 stale，而不是盲目恢复旧结论。

## 6. Verifier 类型

| Verifier | 检查方式 |
| --- | --- |
| `FileContentVerifier` | 文件存在，并且内容精确相等或包含指定文本 |
| `CommandVerifier` | 通过 Workspace 的命令白名单运行 pytest/ruff 等命令 |
| `CompositeVerifier` | 组合多个检查，全部通过才算完成 |

Verifier 不调用大模型，不读取模型的“我完成了”作为证据。命令验收仍复用 Workspace 的无 Shell 白名单，避免 Verifier 变成任意命令后门。

## 7. Benchmark 指标

每个 `BenchmarkCase` 都在新的隔离目录中创建 Fixture，运行 Goal Loop，并由自己的 Verifier 验收。汇总结果包括：

- 通过数、失败数和真实通过率；
- 尝试次数、模型步数、工具调用；
- Prompt/Completion/Total Token；
- 估算费用和累计耗时；
- 每个案例的停止原因和详细 Goal Report 路径。

隔离 Fixture 和确定性 Verifier 保证评测环境可重复；真实大模型本身仍可能有采样随机性，需要在模型侧固定 temperature/seed 或进行多次运行后报告均值和方差。

## 8. 演示中的一波三折

M6 演示要求创建 `app.cfg` 并写入 `mode=prod`：

1. 第一次 Agent 错写为 `mode=dev`，但模型回答“已经写入”。
2. File Verifier 直接读取文件，判定失败。
3. 第一阶段预算只允许一次尝试，因此任务以 `budget_exhausted` 停止并保存 Checkpoint。
4. 第二次使用相同请求恢复；Runtime Identity 和文件 Manifest 均通过校验。
5. GoalRunner 把 `expected mode=prod, got mode=dev` 反馈给模型。
6. Agent 将文件修正为 `mode=prod`，Verifier 通过，状态变为 `succeeded`。
7. Benchmark 另外设置一个“只口头声称完成但没有创建文件”的案例，最终真实通过率为 50%，证明评测不会迎合模型答案。

## 9. 运行方式

```powershell
uv sync
uv run pytest -q
uv run ruff check src tests
uv run picoclaw-m6-demo
```

## 10. 面试一句话

“M6 把 Agent 的自我声明式完成改成外部验证式完成：GoalRunner 在多维预算内组织重试，Verifier 直接检查真实工作区，Checkpoint 通过 Runtime Identity 和文件 SHA-256 防止恢复脏状态，Benchmark 则在隔离 Fixture 上统计真实成功率、Token、费用和耗时。”
