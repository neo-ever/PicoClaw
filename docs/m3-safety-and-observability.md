# M3 安全执行与可观测性导读

## 一次工具调用经过什么

```text
ToolCall
  -> ToolRegistry查找工具
  -> JSON参数校验
  -> 读取RiskLevel
  -> Approver应用ask/auto/never
  -> Workspace路径或命令检查
  -> 执行工具
  -> 统一ToolResult
  -> Trace落盘
```

模型只产生ToolCall，没有直接文件或进程权限。

## 风险等级

| 等级 | 当前工具 | 是否审批 |
| --- | --- | --- |
| `read_only` | `read_file` | 否 |
| `write` | `write_file` | 是 |
| `execute` | `run_shell` | 是 |

## 审批策略

| 策略 | 行为 |
| --- | --- |
| `ask` | 每次写入或执行前询问用户 |
| `auto` | 自动批准风险工具，适合隔离测试环境 |
| `never` | 拒绝所有风险工具，只允许读取 |

审批提示不会打印`write_file.content`原文，只显示UTF-8字节数。

## 文件写入

`Workspace.write_text`先解析真实路径并确认仍在仓库根目录，然后写入同目录临时文件，最后使用原子替换更新目标。内容上限为1MB。

## 受限Shell

`run_shell`设置了以下限制：

- 不使用`shell=True`；
- 拒绝`; & | > <`和换行；
- 拒绝父目录穿越；
- 只允许pytest、ruff check、部分uv run和只读git子命令；
- Python绑定当前虚拟环境解释器；
- 最长运行120秒；
- stdout和stderr分别最多保留30000字符；
- 子进程只继承有限的环境变量，不继承API Key等凭据。

这些规则只是应用层防护，不等于操作系统级沙箱。生产场景仍应将命令放入容器、虚拟机或专用沙箱。

## Trace与Report

每次启用`RunStore`的运行都会创建：

```text
.picoclaw/runs/<run_id>/
├── trace.jsonl
└── report.json
```

`trace.jsonl`按事件追加写入，即使后续步骤失败，前面的轨迹仍然保留。`report.json`在成功或失败结束时原子写入，记录状态、步数、工具调用数、工具错误数、Token Usage和耗时。

Trace只记录工具名称、调用ID、风险、审批和状态，不记录写入文件的完整内容或工具输出全文。
