# PicoClaw 5 分钟演示脚本

## 0. 演示前检查

```powershell
cd E:\PYProject\PicoClaw
uv sync --locked --dev
uv run pytest -q
```

说明：48 个测试用于把 Runtime 正确性与真实模型输出质量分开验证。

## 1. M1：最小 Agent Loop（40 秒）

```powershell
uv run picoclaw-demo
```

讲解：模型先申请读取 README，Runtime 校验并执行，ToolResult 回传后模型才生成最终回答。模型没有直接文件权限。

## 2. M3：安全审批（50 秒）

```powershell
uv run picoclaw-m3-demo --approval never
```

讲解：写文件属于 write 风险。在 never 策略下，Runtime 拒绝调用，工作区没有被修改，同时 Trace 记录拒绝原因。

## 3. M4：记忆命中与失效（60 秒）

```powershell
uv run picoclaw-m4-demo
```

预期观察：

```text
首次查询：tool_calls=1
相同事实再次查询：tool_calls=0
文件变化后：tool_calls=1
```

讲解：第二次从 Episodic Memory 命中；文件 SHA-256 改变后旧事实失效，避免继续复用错误信息。

## 4. M5：Skill 与 MCP（70 秒）

```powershell
uv run picoclaw-m5-demo
```

讲解：Runtime 先根据元数据选择 Skill，只加载命中的正文；真实 MCP stdio Server 只暴露 allowlist 中的 `word_count`，并继续走 ToolRegistry 审批链。

## 5. M6：失败、恢复与外部验收（90 秒）

```powershell
uv run picoclaw-m6-demo
```

预期观察：

```text
第一次：错误写入 mode=dev，Verifier 拒绝，保存 Checkpoint
恢复后：根据失败反馈改成 mode=prod，Verifier 通过
Benchmark：故意失败案例被拒绝，演示通过率 50%
```

讲解：模型的最终回答不是完成证据。成功状态只由读取真实工作区的 Verifier 产生；Checkpoint 还绑定 Runtime Identity 和文件 Manifest。

## 6. 收尾（30 秒）

强调边界：这是可解释、可测试的本地 Agent Runtime 原型。当前证据支持安全执行、上下文、记忆、MCP、恢复和评测机制，不声称生产上线或整体成功率提升。
