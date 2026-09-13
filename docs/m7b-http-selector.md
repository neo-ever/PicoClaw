# M7-B：0.8B HTTP Selector、缓存与降级

## 这一阶段增加了什么

M7-A 使用词法相关度选择代码块，优点是便宜、稳定、无需 GPU，缺点是只能判断“词像不像”。M7-B 新增 `HttpDeltaSelector`，让 0.8B 模型判断：

> 在已经选择的上下文上再加入这个代码块，问题分数增加了多少？

数据流如下：

```text
全部仓库代码块
  -> LexicalSelector 初筛最多 N 个候选
  -> /score_batch 批量计算当前上下文和候选上下文分数
  -> delta = score(已选 + 候选) - score(已选)
  -> 选择最大正 delta
  -> 重复直到预算耗尽或没有正增益
  -> Selected Evidence
```

## 为什么保留词法选择器

0.8B 模型不是 M7-A 的替代品，而是第二级排序器：

| 场景 | 词法选择器的作用 |
| --- | --- |
| 正常运行 | 从数千个代码块缩小到默认 24 个候选 |
| HTTP 超时 | 直接返回词法结果 |
| 服务断开 | 直接返回词法结果 |
| JSON 或分数非法 | 拒绝远程结果并返回词法结果 |
| 所有 Delta 都不为正 | 避免空上下文，返回词法结果 |

这样，学习型模型只影响“证据质量”，不会成为 Agent 能否启动的单点故障。

## HTTP 契约

PicoClaw 调用 Selector 已有的接口：

```http
POST /score_batch
Content-Type: application/json
```

请求：

```json
{
  "items": [
    {
      "context": "[file=src/auth.py lines=1-20]\n...",
      "question": "认证逻辑在哪里实现？"
    }
  ]
}
```

响应：

```json
{
  "scores": [
    {"score": -12.34}
  ]
}
```

Adapter 同时兼容 `scores` 中直接返回数字，但会拒绝：

- 数量与请求不一致；
- `null`、字符串、布尔值；
- `NaN` 和正负无穷；
- 非 JSON 对象或超过响应大小限制的数据。

远程响应只包含分数。代码块 ID、路径和正文始终来自已经过 Workspace 检查的本地候选，远程服务不能伪造一个新路径。

## 批量打分为什么能降低开销

假设词法初筛后有 24 个候选。逐个调用 `/predict_delta`，第一轮需要 24 次 HTTP 请求，而且相同的当前上下文会被重复计算。

M7-B 把当前上下文和所有 `当前上下文 + 候选` 一起交给 `/score_batch`，默认每批 8 条：

```text
25 个 Context / 8 ≈ 4 次 HTTP 请求
```

服务当前仍在一个批量请求内顺序执行模型前向，因此这里主要减少网络往返，并不等于 GPU 真正并行。后续可以在服务端加入 Padding 和 Tensor Batch 获得进一步加速。

## 缓存如何设计

缓存键是：

```text
SHA-256(question + NUL + context)
```

缓存值只有一个浮点分数。这样做有三个目的：

1. 同一次贪心选择中复用上一轮已经算过的上下文；
2. 相同任务再次运行时避免重复请求；
3. 缓存本身不额外保存完整源码文本。

缓存采用有界 LRU，默认最多 2048 项。超出后删除最久没有使用的分数。缓存只存在当前 Python 进程内，进程退出后清空。

## 超时和降级怎么走

```text
HTTP 调用
  ├─ 成功且响应合法 -> 使用 Delta 结果
  └─ 超时/断开/非法响应
       -> 记录 fallback_reason
       -> LexicalSelector 重新选择
       -> Agent 继续运行
```

Trace 新增或扩展的字段包括：

| 字段/事件 | 含义 |
| --- | --- |
| `context_selection_fallback` | 本次发生了词法降级 |
| `cache_hits` | 从缓存取得的分数数量 |
| `cache_misses` | 本次需要远程计算的唯一上下文数量 |
| `http_requests` | 实际发出的批量 HTTP 请求数 |
| `fallback_reason` | 超时、响应错误或无正 Delta 等原因 |

Selector URL、批大小、候选池、Delta 阈值、Evidence 预算和切块配置都会进入 `RuntimeIdentity`。这些配置变化后，Goal Loop 会拒绝恢复旧 Checkpoint，避免用不同的上下文策略继续同一个已保存状态。

## 安全边界

默认只允许 `localhost`、`127.0.0.1` 或 `::1`。非回环地址会在配置阶段被拒绝，因为调用 Selector 会发送初筛后的仓库代码。

只有用户显式提供 `--allow-remote-selector` 才允许远程地址。即使开启：

- `.env`、常见私钥和凭据文件仍被 Chunker 跳过；
- Selector 仍没有任何工具执行权限；
- 返回值仍经过类型、长度和有限数值检查；
- HTTP 失败仍会降级到完全本地的词法选择。

这不是完整的内容级 Secret Scanner。敏感仓库优先使用本地部署的 Selector。

## 运行方式

先在有模型的机器运行原 Selector 服务：

```powershell
cd selector\2B
$env:QWEN0P8B_DIR="你的Qwen3.5-0.8B模型目录"
$env:PORT="6006"
python serve_2b.py
```

再运行 PicoClaw：

```powershell
cd E:\PYProject\PicoClaw
$env:PICOCLAW_SELECTOR_URL="http://127.0.0.1:6006"
uv run picoclaw-real `
  --evidence `
  --evidence-selector http `
  --evidence-tokens 1200 `
  --selector-batch-size 8 `
  --selector-candidate-pool 24 `
  "认证逻辑在哪里实现？"
```

无需 GPU 的确定性演示：

```powershell
uv run picoclaw-m7b-demo
```

演示中的 Transport 模拟 `/score_batch` 契约，用于验证 PicoClaw 的编排逻辑，不冒充真实 0.8B 推理效果。

## 关键代码入口

| 文件 | 作用 |
| --- | --- |
| `selection/http.py` | URL 安全校验、JSON Transport、批量评分、贪心 Delta、降级 |
| `selection/cache.py` | 线程安全的有界 LRU 分数缓存 |
| `selection/models.py` | 缓存、HTTP 和降级审计字段 |
| `real_demo.py` | CLI 参数与 Selector 构造 |
| `agent.py` | Trace 和 Report 记录 |
| `checkpoint.py` | Selector 配置哈希和递归源码实现哈希 |
| `m7b_demo.py` | 无 GPU 的端到端演示 |

## 事实边界

原服务在线计算的是 `p(question|context)`，而已报告的训练实验使用答案 Token Log Probability。M7-B 完成的是可靠的工程接入，不能据此声称：

- 0.8B Selector 在代码任务上已有 84.1% 准确率；
- Coding Agent 任务成功率已经提高；
- 当前方案优于 BM25、Reranker 或全上下文。

这些结论需要在 M7-C 中统一训练与推理目标、构造代码领域数据，并使用固定 Reader 与外部 Verifier 做 A/B Benchmark。
