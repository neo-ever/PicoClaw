# M5：渐进式 Skills 与受控 MCP Adapter

## 1. 这一阶段解决什么问题

M4 已经能控制上下文长度并复用记忆，但仍有两个扩展性问题：

1. 如果把所有领域操作手册都塞进 Prompt，上下文会越来越贵，而且无关规则会互相干扰。
2. 如果每接一个外部工具都手写一套 Provider，工具生态无法扩展；但直接信任外部 MCP Server 又会绕过本地安全边界。

M5 的目标不是让模型拥有更多权限，而是让 Runtime 能够用统一方式发现“做事方法”和“外部工具”，再把它们纳入既有治理链。

## 2. 大白话理解

Skill 像书架上的操作手册。PicoClaw 启动时只看每本书封面的名称和简介，收到任务后才取出最相关的一两本阅读。这样不用把整座书架都搬进模型上下文。

MCP 像通用插座。外部 Server 告诉 PicoClaw 自己有哪些工具、每个工具需要哪些参数；PicoClaw 把允许使用的工具转换成内部 `Tool`。插上插座不等于获得无限权限，真正调用时仍要经过参数校验、风险判断和人工审批。

## 3. Skills 执行链路

| 阶段 | 代码 | 行为 |
| --- | --- | --- |
| 扫描 | `SkillCatalog.scan()` | 只逐行读取 frontmatter 中的 `name/description` |
| 匹配 | `SkillCatalog.select()` | 用请求与元数据的词项重合度做透明、可复现的排序 |
| 加载 | `SkillCatalog.load()` | 只读取命中的正文，并检查根目录边界和文件大小 |
| 注入 | `ContextManager._skill_message()` | 放进独立 Token 预算的 system 消息 |
| 审计 | `Agent.run()` | 写入 `skills_selected` Trace 和最终 Report |

Skill 是指导信息，不是授权信息。注入消息会明确声明：Skill 不能绕过工具白名单、参数检查、工作区边界和审批策略。

## 4. MCP 执行链路

```text
操作员提供 MCPServerConfig
        ↓
启动 stdio Client 并 list_tools
        ↓
只保留 allowed_tools 中的工具
        ↓
转换成 mcp__<server>__<tool> 内部 Tool
        ↓
模型申请调用
        ↓
ToolRegistry 参数校验 + 风险审批
        ↓
重新建立 MCP Client → call_tool → 关闭 Client
        ↓
ToolResult → Trace → 模型下一轮决策
```

`MCPServerConfig` 是由程序操作者配置的，不接受模型动态修改。未写入 `allowed_tools` 的工具即使被 Server 广播，也不会暴露给模型。MCP 工具默认风险为 `execute`，可由操作员逐项显式调整。

## 5. 为什么每次调用重新连接

官方 SDK 的 `Client` 是异步上下文管理器，进入上下文时连接，退出时断开，不能在退出后继续复用。本阶段选择“发现一次连接、每次调用一次新连接”，优点是生命周期简单、不会留下僵尸 Session，缺点是高频调用会增加进程启动延迟。后续可以用后台事件循环和 Session Pool 优化，但必须同时解决并发、崩溃恢复和关闭顺序。

## 6. 安全边界

| 风险 | M5 的处理 |
| --- | --- |
| Skill Prompt 注入 | 标记为不可信指导，不能改变 Runtime 权限 |
| Skill 越界/超大文件 | 根目录检查、元数据与正文大小上限 |
| MCP 工具泛滥 | 显式 allowlist，缺失工具直接报错 |
| 工具重名 | `mcp__server__tool` 命名空间 |
| 参数错误 | 复用 `ToolRegistry` 的 JSON Schema 校验 |
| 危险执行 | 默认 `execute`，复用 ask/auto/never 审批 |
| 结果过大 | 字符上限与截断标记 |
| 多媒体泄漏上下文 | 图片/音频只返回类型占位，不注入 Base64 |
| 子进程环境泄漏 | 只向 SDK 传入显式配置的附加环境变量 |

## 7. 运行与观察

```powershell
uv sync
uv run pytest -q
uv run ruff check src tests
uv run picoclaw-m5-demo
```

演示使用真实 MCP SDK 启动 `picoclaw.mcp_demo_server`，发现并调用 `word_count`。最终可在 `.picoclaw/m5-demo-runs/<run_id>/` 查看 `trace.jsonl` 和 `report.json`。

## 8. 面试时一句话总结

“M5 把能力扩展拆成了知识扩展和工具扩展：Skills 通过元数据检索后按需加载，降低上下文开销；MCP Adapter 通过 allowlist 和 Schema 映射接入外部工具，同时复用 Runtime 的风险审批与审计链，所以扩展能力没有破坏原有安全边界。”
