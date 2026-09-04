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

## 结果边界

- 这是本地离线验证记录，不代表生产可用性、线上 SLA 或真实用户规模。
- GitHub Actions 已配置 Python 3.12/3.13 测试矩阵，但远端 CI 只有推送仓库后才会产生运行记录。
- M6 Benchmark 的 50% 是由一个通过案例和一个故意失败案例构成，用于证明 Verifier 会拒绝模型的无证据成功声明，不是模型能力评测分数。
