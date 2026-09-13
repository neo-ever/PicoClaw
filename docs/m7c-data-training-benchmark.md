# M7-C：数据、训练与 A/B 评测闭环

## 最终解决的问题

M7-B 工程上接入了原 Selector 的 `p(question|context)` 服务，但原训练实验使用 `p(answer|context, question)`。M7-C 不再用两个不同目标解释同一个指标，而是定义统一契约：

```text
输入：任务、当前证据、候选代码块
标签：加入候选后，固定 Reader + 外部 Verifier 的分数变化

delta = verifier_score(plus_context) - verifier_score(minus_context)
```

训练和在线推理都预测这个 Delta。正数表示候选有帮助，零或负数表示不应加入。

## C1：多策略 Context Benchmark

`context_benchmark.py` 对每个“案例 × 策略”创建独立工作区，写入完全相同的 Fixture，再运行 Agent 并调用外部 Verifier。这样一个策略修改的文件不会污染另一个策略。

统一报告包括：

| 维度 | 指标 |
| --- | --- |
| 任务质量 | Verifier 通过率、失败类型 |
| Agent 成本 | 模型步数、工具调用、文件读取 |
| Token | Prompt、Completion、Evidence、Rendered Context |
| 选择质量 | 候选数、选中数、压缩率、重复率 |
| HTTP | 请求数、缓存命中/未命中、降级次数、延迟 |

内置 `FullContextSelector` 不是推荐生产策略，而是受相同 Evidence 预算约束的对照组。

## C2：统一数据契约

数据 Schema 为 `picoclaw-delta-v1`：

```json
{
  "schema_version": "picoclaw-delta-v1",
  "example_id": "...",
  "task_id": "fix-auth-001",
  "query": "修复 refresh token 过期判断",
  "minus_context": "...",
  "candidate_context": "...",
  "plus_context": "...",
  "score_minus": 0.0,
  "score_plus": 1.0,
  "delta": 1.0,
  "label": 1,
  "source": "reader-verifier-v1",
  "metadata": {"candidate_path": "src/auth.py"}
}
```

构造时强制检查：

- `delta == score_plus - score_minus`；
- 所有分数必须是有限数；
- `label` 必须与 Delta 正负一致；
- Schema 版本必须匹配；
- 训练/验证按 `task_id` 分组切分，不能按单条样本随机切分。

最后一条用于防止同一任务的正负候选同时出现在训练集和验证集，造成虚高指标。

## C3：代码领域数据构造

`DeltaDatasetBuilder` 不绑定具体模型，而是依赖 `OutcomeScorer`：

```python
class OutcomeScorer(Protocol):
    def score(self, query: str, context: str) -> float: ...
```

正式数据应让 Scorer 执行固定 Reader，再让 File/Command/Composite Verifier 检查真实工作区。离线演示使用 Marker Scorer，只为证明数据管道和泄漏保护可运行。

JSONL 使用临时文件加 `os.replace()` 原子落盘，避免中断后留下半个数据文件。

## C4：两条训练路径

### 轻量 Linear Delta

`selection/linear.py` 提取五个可解释特征：

1. Query 与 Candidate 的词覆盖率；
2. Query 与 Plus Context 的词覆盖率；
3. Candidate 相对当前证据的新颖度；
4. Candidate 与当前证据的重复度；
5. Candidate 长度。

模型用纯 Python 梯度下降拟合 Delta，输出 Sign Accuracy、MAE、RMSE 和 Pearson。它的用途是验证训练、保存、加载和在线选择闭环，不代替 Qwen。

### Qwen 0.8B Direct Delta

`qwen_delta_train.py` 使用 `AutoModelForSequenceClassification(num_labels=1)` 做回归。训练和服务共同调用 `format_delta_prompt()`，因此 Prompt 契约只有一个实现。

训练命令：

```powershell
uv run picoclaw-qwen-delta-train `
  --model "你的Qwen3.5-0.8B模型目录" `
  --train .picoclaw/delta-data/train.jsonl `
  --validation .picoclaw/delta-data/validation.jsonl `
  --output .picoclaw/models/qwen-delta `
  --max-length 2048 `
  --batch-size 1 `
  --gradient-accumulation 16
```

该命令需要 GPU 环境中的 `torch`、`transformers`、`accelerate` 和 `numpy`。

仓库已提供 `qwen` 可选依赖和训练前预检：

```powershell
uv sync --extra qwen
uv run picoclaw-qwen-preflight `
  --model "你的Qwen模型目录或模型ID" `
  --train .picoclaw/delta-data/train.jsonl `
  --validation .picoclaw/delta-data/validation.jsonl
```

预检会阻止缺少依赖、无 CUDA、数据不可读、任务泄漏或训练标签只有单一类别的配置。完整迁移步骤见 `m7c-gpu-handoff.md`。

服务命令：

```powershell
uv run picoclaw-qwen-delta-serve `
  --model .picoclaw/models/qwen-delta `
  --host 127.0.0.1 `
  --port 6007 `
  --max-batch-size 32
```

服务使用一次 Padding 后的 Tensor Batch，而不是在 `/score_batch` 内用 Python 循环逐条前向。

PicoClaw 客户端使用：

```powershell
uv run picoclaw-real `
  --evidence `
  --evidence-selector http-direct `
  --selector-url http://127.0.0.1:6007 `
  "修复认证逻辑"
```

## C5：本机实际运行结果

运行命令：

```powershell
uv run picoclaw-m7c-demo
```

本次确定性合成实验结果：

| 项目 | 结果 |
| --- | --- |
| 训练样本 | 18 |
| 验证样本 | 6 |
| 训练/验证任务交集 | 0 |
| Linear 验证 Sign Accuracy | 100% |
| Linear 验证 MAE | 0.0003 |
| Linear 验证 Pearson | 1.0000 |

策略结果：

| 策略 | Verifier 通过率 | 文件读取 | Prompt Token | Evidence Token |
| --- | ---: | ---: | ---: | ---: |
| none | 100% | 3 | 190 | 0 |
| lexical | 100% | 0 | 573 | 40 |
| http-simulated | 100% | 0 | 573 | 40 |
| full | 100% | 0 | 1131 | 97 |
| linear-trained | 100% | 0 | 573 | 40 |

这个结果展示了真实的工程权衡：预加载 Evidence 消除了读取文件调用，但增加了首轮 Prompt；Full Context 的 Prompt 又明显大于精简选择。所有案例都通过，所以这组数据不能证明哪种策略提高了准确率。

## 事实边界与未完成条件

已经完成并实跑：

- 多策略隔离 Benchmark；
- 外部文件 Verifier；
- Direct Delta Schema、JSONL、分组切分；
- Linear 训练、验证、保存和在线选择；
- Direct Delta HTTP 客户端和服务端契约；
- 真实本机 HTTP wire test；
- Qwen 训练与批量服务程序的静态、导入和 CLI 检查。

当前无法实跑：

- Qwen 0.8B 微调；
- 真实 Qwen Checkpoint 推理；
- 真实 Coding Agent 公开任务集结论。

原因是当前机器没有 NVIDIA GPU、项目 uv 环境缺少 `torch/transformers`，也没有本地 0.8B 权重。必须把这项状态写成“训练入口完成、实际训练待 GPU”，不能把合成 Linear 指标移植到 Qwen 或真实 Agent。
