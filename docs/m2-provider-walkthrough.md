# M2 Provider 导读

## 为什么需要Provider

不同模型服务返回的工具格式不同：OpenAI-compatible接口返回结构化`tool_calls`，部分本地Ollama模型只能按照Prompt输出文本标签。PicoClaw不能让这种差异扩散到Agent Loop，因此Provider承担“翻译器”角色。

```text
厂商响应 -> Provider翻译 -> ModelResponse -> Agent Loop
```

## OpenAI-compatible路径

1. 将`Message`转换成Chat Completions消息。
2. 把`ToolRegistry`提供的JSON Schema放入`tools`参数。
3. 调用SDK。
4. 解析`message.tool_calls[].function.arguments`。
5. 把JSON字符串转换成`dict`。
6. 生成统一`ToolCall`和`TokenUsage`。

## Ollama回退路径

1. 将工具Schema与历史记录渲染进Prompt。
2. 调用本地`/api/generate`。
3. 只接受`<tool>{...}</tool>`或`<final>...</final>`。
4. 非法JSON或缺少标签时抛出`ProviderProtocolError`。

## 为什么保留工具消息对

模型返回工具申请后，下一轮历史必须同时包含：

```text
assistant.tool_calls(id=call-1)
tool(tool_call_id=call-1, result=...)
```

如果压缩上下文时拆开这两条消息，模型会看到一个没有结果的工具调用，或者一个找不到来源的工具结果。后续M4上下文压缩必须把这组消息当作不可拆分的原子组。

## 安全边界

Provider只负责协议翻译，不执行工具。即使模型返回合法ToolCall，也必须经过ToolRegistry和Workspace检查。
