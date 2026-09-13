# Changelog

本项目遵循语义化版本思路记录重要变化。

## Unreleased

### Added

- M7-A 安全仓库切块器：记录文件路径、行号、Token 数和来源 SHA-256。
- 可替换 `ContextSelector`/`EvidenceProvider` 协议及本地 BM25 风格词法选择器。
- 独立 Evidence Token 预算、ContextManager 注入、Trace 与 Report 选择审计。
- `picoclaw-real --evidence` 可选入口和 `picoclaw-m7-demo` 确定性离线演示。
- Qwen 0.8B `/score_batch` HTTP Adapter、词法候选池与边际增益贪心选择。
- 不保存源码正文的有界 LRU 分数缓存，以及超时、连接和响应校验失败后的词法降级。
- 默认仅允许本机 Selector；远程发送候选代码需要显式授权。
- Selector 与切块配置纳入 Runtime Identity，避免配置变化后恢复不兼容的 Checkpoint。
- `picoclaw-m7b-demo` 离线展示批量请求、缓存命中和超时降级。
- `picoclaw-delta-v1` 代码 Pair Schema、OutcomeScorer 标注接口、原子 JSONL 和任务级数据切分。
- Direct Delta HTTP Selector 与 `/predict_delta_batch` Qwen Regression 服务契约。
- 无 PyTorch 的 Linear Delta 训练、评估、持久化和在线选择基线。
- 隔离工作区的多策略 Context Benchmark，统一记录 Verifier、Token、工具调用、压缩率、缓存和降级指标。
- Qwen 0.8B Sequence Classification Regression 训练入口和真正批量推理服务。
- M7-C 离线演示串联数据构造、训练、验证和五策略 A/B 报告。
- `qwen` 可选依赖组、训练环境/数据预检 CLI 和 GPU 交接清单。
- 显式声明 Qwen3.5 Trainer 的 `labels` 字段，修复验证指标缺失导致的最佳模型选择失败。
- 同步 Tokenizer、Qwen3.5 顶层配置与文本配置的 PAD Token，支持真正的批量回归推理。
- Qwen 训练新增 `--head-only` 冻结骨干选项，M7-C Demo 支持扩大合成任务规模，用于低成本验证新回归头的可学习性。

### Known limitations

- Direct Delta 契约已经统一训练与在线推理目标，但尚未在正式代码领域数据上验证实际提升。
- 当前机器没有 CUDA GPU，项目 uv 环境没有 PyTorch/Transformers，且没有本地 0.8B 权重，因此尚无真实 Qwen 代码领域微调指标。
- M7-C 演示数据和 Reader 是确定性合成 Fixture，只证明闭环实现，不代表真实 Coding Agent 提升。

## 0.1.0 - 2026-09-06

### Added

- Provider 无关的 Agent Loop 与统一 ToolCall/ToolResult 协议。
- OpenAI-compatible 原生 Function Calling 与 Ollama 文本协议回退。
- 工作区路径边界、工具参数校验、风险等级和 ask/auto/never 审批。
- 原子文件写入、开发命令白名单、Trace 与 Run Report。
- Token 感知上下文压缩与 Working/Episodic/Durable 三层记忆。
- 基于文件 SHA-256 的记忆失效。
- 渐进式 Skills 与受控 MCP stdio Adapter。
- 多维预算 Goal Loop、可信 Checkpoint、独立 Verifier 与隔离 Benchmark。
- M1–M6 离线演示、48 个自动化测试和 Python 3.12/3.13 CI 配置。

### Known limitations

- 当前定位为本地学习原型，没有生产部署、真实用户或 SLA 数据。
- MCP 每次调用重新建立连接，高频工具调用会有额外启动开销。
- Benchmark 以确定性本地案例为主，尚未接入公开 Coding Agent 任务集。
- Checkpoint Manifest 默认最多扫描 5000 个非运行工件文件，并跳过符号链接。
