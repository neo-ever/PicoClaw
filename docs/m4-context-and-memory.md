# M4 上下文与三层记忆导读

## 上下文为什么需要预算

工具结果和历史会随着Agent Loop不断增长。M4在每次调用模型前计算本地Token预算，并生成`ContextSnapshot`：

```text
原始Messages
  -> 注入相关Memory
  -> 将assistant tool_calls与tool results组成原子组
  -> 从最近历史向前保留
  -> 压缩被移除的旧历史
  -> 保证Current Request不被截断
```

已知模型使用`tiktoken.encoding_for_model`；未知模型使用`o200k_base`回退。文本由真实Tokenizer切分，但Chat Completions消息封装和非OpenAI模型仍属于估算，不能冒充服务端精确Token。

## 为什么工具消息不能拆开

下面两条消息共同构成一次工具交互：

```text
assistant.tool_calls(id=call-1)
tool(tool_call_id=call-1, content=...)
```

如果只保留一条，API可能拒绝请求，模型也无法理解工具结果来自哪里。因此Context Manager以组为单位保留或丢弃。

## 三层记忆

| 层级 | 生命周期 | 当前内容 |
| --- | --- | --- |
| Working | 当前任务 | 本轮读取的文件摘录 |
| Episodic | 跨任务持久化 | 文件摘录、任务与结果摘要 |
| Durable | 显式长期保存 | 项目约定、稳定事实和用户偏好 |

Working在新任务开始时清空；任务成功后，高价值Working记录进入Episodic。Durable只通过显式`add_durable`写入，避免模型自动把猜测永久保存。

## 文件新鲜度

每条文件记忆保存：

```text
source_path + SHA-256 + content
```

检索前Runtime重新计算当前文件哈希：

```text
哈希一致 -> 允许注入
哈希变化/文件消失 -> 删除旧记忆与依赖该文件的任务结论
```

`write_file`成功后也会立即使该路径的旧记忆失效。哈希检查仍会读取文件字节，只是避免了一次由模型发起的`read_file`工具轮次。

## 检索

当前实现采用透明的轻量规则：请求与记忆的词项重合、文件路径匹配、记忆层级权重，最多选择3条。它不是向量检索，适合当前小型项目；M5之后可以替换为BM25、Embedding或混合检索。

## 持久化

Episodic与Durable写入：

```text
.picoclaw/memory.json
```

Working只存在于内存。每次运行的`report.json`额外记录上下文构建次数、估算Token、丢弃历史组数、请求保护率、工具组完整率和失效记忆数。
