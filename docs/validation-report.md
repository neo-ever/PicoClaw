# PicoClaw v0.1.0 本地验证记录

验证日期：2026-09-04

验证环境：Windows / Python 3.13.3 / uv 0.11.26

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 锁文件同步 | `uv sync --locked --dev` | 通过 |
| 格式检查 | `uv run ruff format src tests --check` | 29 个文件格式正确 |
| 静态检查 | `uv run ruff check src tests` | 通过 |
| 自动化测试 | `uv run pytest -q` | 48 passed |
| 源码包与 Wheel | `uv build` | 成功生成 `.tar.gz` 和 `.whl` |
| sdist 内容 | 检查压缩包文件列表 | 包含 README、docs 和 examples |
| 隔离 Wheel 导入 | 从临时目录通过 `uv run --isolated --with <wheel>` 导入 | 成功 |
| 隔离 Wheel 演示 | 从临时目录运行 `picoclaw-m5-demo` | MCP 调用成功，返回 9 words |
| M6 端到端 | `uv run picoclaw-m6-demo` | 1 次失败后 Checkpoint 恢复，第 2 次通过 |

## M7-A 增量验证（2026-09-06）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 静态检查 | `uv run ruff check src tests` | 通过 |
| 自动化测试 | `uv run pytest -q` | 53 passed |
| M7-A 端到端 | `uv run picoclaw-m7-demo` | 从 3 个候选文件中选中 `src/auth.py:1-2`，Evidence 16/240 Token，0 次工具调用 |

M7-A 的演示结果证明本地词法选择与 Evidence 注入链路可运行，不代表在真实 Coding Agent Benchmark 上已经提高准确率。

## M7-B 增量验证（2026-09-07）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 格式检查 | `uv run ruff format src tests --check` | 41 个文件格式正确 |
| 静态检查 | `uv run ruff check src tests` | 通过 |
| 自动化测试 | `uv run pytest -q` | 62 passed |
| HTTP Wire Contract | 本地临时 HTTP Server 调用真实 `UrllibJsonTransport` | `/score_batch` 请求与响应通过 |
| M7-B 端到端 | `uv run picoclaw-m7b-demo` | 首轮批量请求、第二轮 0 HTTP 缓存命中、模拟超时后词法降级均通过 |

M7-B 验证覆盖的是 HTTP 编排、批量、缓存、响应校验、网络安全策略和降级逻辑。离线演示使用模拟分数，没有加载 0.8B 权重，因此不代表真实模型的选择质量。

## M7-C 增量验证（2026-09-13）

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 格式检查 | `uv run ruff format src tests --check` | 50 个文件格式正确 |
| 静态检查 | `uv run ruff check src tests` | 通过 |
| 自动化测试 | `uv run pytest -q` | 71 passed |
| 数据隔离 | M7-C Demo grouped split | 18 train / 6 validation，任务交集 0 |
| 轻量训练 | M7-C Linear Delta | 合成验证 Sign Accuracy 100%、MAE 0.0003、Pearson 1.0000 |
| 策略对照 | `uv run picoclaw-m7c-demo` | 5 策略 × 3 隔离案例，外部 File Verifier 全部通过 |
| 0.8B 资源审计 | `picoclaw-qwen-preflight` | uv 环境缺少训练依赖、无 NVIDIA GPU、无本地模型权重；数据 Schema/隔离/标签平衡通过，未运行真实微调 |
| GPU 交接 | 可选依赖、Preflight CLI、操作清单 | 训练前依赖、CUDA、模型、数据泄漏和标签检查已可自动执行 |
| AutoDL 首次实跑 | Qwen3.5-0.8B-Base，18/6 合成数据 | 训练完成 5/5 步；发现并修复 Transformers 5.17 未自动推断通用分类器 `labels` 的验证指标问题 |
| AutoDL 批量推理 | Qwen3.5-0.8B-Base，2 条 Tensor Batch | 修复复合配置 PAD Token 后返回 2 个真实 Delta；首次模型 Sign Accuracy 为 50%，未达到质量目标 |

## M7-C GPU 学习探针与 M8 收尾验证（2026-09-13 至 2026-09-14）

| 检查 | 配置 | 结果 |
| --- | --- | --- |
| Head-only 训练 | Qwen3.5-0.8B-Base；300 train / 100 validation；5 epochs | 仅训练 1,024 / 852,986,944 参数；112.74 秒完成 |
| 合成验证 | Direct Verifier Delta Regression | Sign Accuracy 100%、MAE 0.4158、RMSE 0.4306、Pearson 0.9943 |
| 批量服务 | `/predict_delta_batch`，CUDA，batch=2 | 相关合成样本 `+0.5078`，无关合成样本 `-1.4922` |
| Selector 接入 | `DirectDeltaHttpSelector` | `fallback_used=false`，选中相关样本，证明训练—服务—Runtime 链路贯通 |
| 真实仓库负例 | 中文仓库介绍任务 | 暴露 `.venv-autodl` 未剪枝、Selector 耗时 514.37 秒并选择第三方依赖的问题 |
| 无 Selector Agent 复测 | DeepSeek compatible provider，只读仓库介绍 | 第 6 个模型步骤完成，但共 19 次工具调用、11 次失败、累计 42,069 Token |
| M8 自动化测试 | `uv run pytest -q` | 73 passed |
| M8 静态检查 | `uv run ruff check src tests` | 通过 |

M8 根据真实运行负例增加了虚拟环境目录剪枝、安全文件发现和范围读取。由于 GPU 实例已关机，尚未声称这些改动已经带来真实模型端的量化下降；上表保留优化前数据作为后续复测基线。

合成 Linear 指标只证明训练与评测管道能发现刻意构造的相关代码块，不是 Qwen 指标，也不是公开 Coding Agent Benchmark 结果。

## 结果边界

- 这是本地离线验证记录，不代表生产可用性、线上 SLA 或真实用户规模。
- Head-only 的 100% Sign Accuracy 来自按任务隔离的合成数据，只能证明回归头在该数据分布上可学习。
- GitHub Actions 已配置 Python 3.12/3.13 测试矩阵，但远端 CI 只有推送仓库后才会产生运行记录。
- M6 Benchmark 的 50% 是由一个通过案例和一个故意失败案例构成，用于证明 Verifier 会拒绝模型的无证据成功声明，不是模型能力评测分数。
