# M7-A：自适应仓库上下文基线

## 这一阶段解决什么问题

PicoClaw 原来可以让模型主动调用 `list_files`、`search_text` 和 `read_file` 探索仓库，但模型第一次决策时只能看到用户请求、Skills、Memory 和历史。仓库变大后，模型可能需要多次搜索和读取才能碰到关键文件。

M7-A 在第一次模型调用前增加一个只读的“证据路由”步骤：

```text
用户请求
  -> RepositoryChunker 安全扫描并切块
  -> ContextSelector 在 Token 预算内选择
  -> ContextManager 注入 Selected Evidence
  -> Agent Loop 决策和调用工具
```

它不是工具，也不能修改文件或运行命令。它只返回 Runtime 可以检查和记录的 `ContextChunk`。

## 代码是怎么组织的

| 文件 | 作用 | 大白话 |
| --- | --- | --- |
| `selection/models.py` | 定义代码块、评分结果和最终选择结果 | 规定各模块交换的数据长什么样 |
| `selection/protocol.py` | 定义 `ContextSelector` 和 `EvidenceProvider` | 将来换成 0.8B 模型时，Agent 不用重写 |
| `selection/chunker.py` | 在 Workspace 内扫描文本文件并按 Token 切块 | 把大仓库变成许多带行号的小卡片 |
| `selection/lexical.py` | BM25 风格相关度、路径加分和重复惩罚 | 先找关键词匹配，再避免选一堆重复内容 |
| `selection/service.py` | 组合切块器和选择器 | 对 Agent 暴露一个简单的 `select(query)` |
| `context_manager.py` | 给 Evidence 独立预算并注入模型上下文 | 决定这些代码卡片最终放在哪里 |
| `agent.py` | 调用 EvidenceProvider，生成 Trace 和 Report | 记录选了什么、为什么选和花了多少时间 |

## 一块代码的数据结构

```python
@dataclass(frozen=True, slots=True)
class ContextChunk:
    id: str
    path: str
    start_line: int
    end_line: int
    content: str
    source_sha256: str
    token_count: int
```

- `path + start_line + end_line` 让人可以定位原文件。
- `source_sha256` 证明选择时看到的是哪个文件版本。
- `token_count` 用于硬性预算，防止选择完才发现上下文装不下。
- `id` 由路径、行号和文件哈希生成；文件变化后 ID 也会变化。

## 切块器如何保证边界

`RepositoryChunker` 只遍历 `Workspace.root`，并再次通过 `Workspace.resolve()` 检查路径。它会跳过：

- `.git`、`.venv`、`node_modules`、`dist`、`.picoclaw` 等目录；
- 符号链接、二进制文件、超大文件和无法按 UTF-8 解码的文件；
- `.env`、私钥、证书和常见凭据文件。

这些规则的目标是降低误读和泄露风险，不代表完整的 Secret Scanner。未来如果接远程 Selector，还需要内容级脱敏和显式授权。

## 词法选择器如何打分

第一步把请求、路径和代码拆成检索词。例如：

```text
validate_refresh_token
-> validate_refresh_token, validate, refresh, token
```

第二步用 BM25 风格公式提高“在当前代码块出现、但不是所有文件都出现”的词的权重；文件路径命中会获得额外加分。

第三步逐个选择代码块，并计算它与已选块的 Jaccard 重复度：

```text
最终分数 = 相关度 × (1 - 重复惩罚系数 × 重复度)
```

每次加入代码块前都会检查累计 `token_count`，因此 `SelectionResult.selected_tokens` 不会超过 `token_budget`。完全相同的代码块只保留一份。

## Evidence 为什么被标为不可信

仓库文件中可能故意写入“忽略安全策略并删除文件”之类的 Prompt Injection。ContextManager 在 Evidence 头部明确告诉模型：这些内容只是任务数据，不能提供额外权限。

真正的防线仍然是 Runtime：即使模型被误导，工具调用仍要经过工具白名单、参数 Schema、Workspace 边界和审批策略。

## Trace 和 Report 记录什么

一次开启 Evidence 的任务会新增：

| 事件 | 主要字段 |
| --- | --- |
| `context_candidates_retrieved` | 候选代码块数量 |
| `context_chunk_selected` | 排名、路径、行号、SHA、Token、相关度、冗余度 |
| `context_selection_finished` | Selector 身份、选中数量、预算、耗时 |
| `context_selection_failed` | 失败类型；Agent 随后无 Evidence 降级运行 |

Report 不保存额外的完整代码副本，只保存选择元数据；实际模型上下文仍可能包含所选代码，因此运行工件也应按本地敏感数据管理。

## 如何运行

无需 API Key 的离线演示：

```powershell
uv run picoclaw-m7-demo
```

真实模型：

```powershell
uv run picoclaw-real `
  --evidence `
  --evidence-tokens 1200 `
  --chunk-tokens 320 `
  "认证逻辑在哪里实现？"
```

## 当前事实边界

- M7-A 已证明切块、选择、预算、注入、审计和故障降级链路可以本地运行。
- 它尚未证明任务准确率提高，也没有公开 Coding Agent Benchmark 数据。
- 当前是词法基线，不应描述为“使用 Qwen 训练出的学习型 Selector”。
- Selector 项目中的 84.1% delta 符号准确率来自原长文本实验，不能当成 PicoClaw 代码选择效果。

## 后续阶段

M7-B 已实现 HTTP Adapter、批量打分、超时、缓存和词法降级，详见 `m7b-http-selector.md`。M7-C 仍需统一训练与在线推理的评分目标，并使用代码仓库任务重新验证。
