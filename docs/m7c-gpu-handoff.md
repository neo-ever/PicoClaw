# M7-C GPU 训练交接清单

这份清单只处理本机无法完成的真实 Qwen Direct Delta 训练。先完成预检，再启动训练；不要跳过数据隔离检查。

## 1. 当前已经准备好的内容

| 项目 | 入口 |
| --- | --- |
| 统一训练数据 Schema | `selection/dataset.py` |
| Qwen 回归训练 | `qwen_delta_train.py` |
| 批量推理服务 | `qwen_delta_service.py` |
| 环境与数据预检 | `qwen_preflight.py` |
| Agent HTTP 接入 | `DirectDeltaHttpSelector` |
| 外部验收 | `ContextBenchmarkRunner` |

## 2. 你需要准备的外部资源

| 资源 | 最低要求 | 为什么必须由你准备 |
| --- | --- | --- |
| GPU 机器 | NVIDIA GPU，建议至少 16 GB 显存 | 当前电脑没有 CUDA GPU |
| 基础模型 | 合法取得的 Qwen 0.8B 本地目录或可下载模型 ID | 当前两个项目中没有模型权重 |
| 正式代码任务 | 至少 100 个任务，训练/验证按 `task_id` 隔离 | 当前 24 条是合成冒烟数据，不能得出性能结论 |
| Reader/Verifier | 固定版本、固定参数 | Delta 标签必须来自同一个结果判定标准 |

## 3. 在 GPU 机器上安装

普通 GPU 环境可在克隆或复制仓库后执行：

```bash
uv sync --extra qwen
uv run picoclaw-qwen-delta-train --help
uv run picoclaw-qwen-delta-serve --help
uv run picoclaw-qwen-preflight --help
```

如果 `torch.cuda.is_available()` 仍然为 `False`，按照 GPU 机器对应的 CUDA/驱动版本安装 PyTorch，之后重新运行预检；不要在 CPU 上正式训练 0.8B 模型。

### AutoDL 已预装 PyTorch 的环境

AutoDL 镜像已经提供与驱动匹配的 PyTorch 时，不要用项目锁文件覆盖它。创建一个能够读取系统包的虚拟环境，只安装其余依赖：

```bash
cd /root/autodl-tmp/PicoClaw
uv venv --python 3.12 --system-site-packages
source .venv/bin/activate
uv pip install -e .
uv pip install "accelerate>=1.2,<2" "numpy>=2,<3"
uv pip install "transformers>=5.17,<6"
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.version.cuda)"
```

Qwen3.5 的 Sequence Classification 支持晚于早期 Transformers 版本。本项目使用已经包含该支持的 5.17 系列，Preflight 还会单独检查 `Qwen3_5ForSequenceClassification` 是否存在。

## 4. 先做一次合成数据冒烟测试

在 PicoClaw 根目录运行：

```powershell
picoclaw-m7c-demo
$run = Get-ChildItem .picoclaw -Directory -Filter "m7c-demo-*" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1
$train = Join-Path $run.FullName "data\train.jsonl"
$validation = Join-Path $run.FullName "data\validation.jsonl"
```

然后检查环境、模型和数据：

```powershell
picoclaw-qwen-preflight `
  --model "你的Qwen模型目录或模型ID" `
  --train $train `
  --validation $validation
```

只有输出 `ready=true` 才进入下一步。合成数据少于 100 条时会显示 `WARN`，它不阻止冒烟训练，但不能用于简历性能数字。

## 5. 正式训练

将 `$train` 和 `$validation` 替换为正式数据路径：

```powershell
picoclaw-qwen-delta-train `
  --model "你的Qwen模型目录或模型ID" `
  --train $train `
  --validation $validation `
  --output .picoclaw/models/qwen-delta `
  --max-length 2048 `
  --epochs 1 `
  --batch-size 1 `
  --gradient-accumulation 16
```

完成标志是模型目录内出现 `delta-training-report.json`，并记录验证集 `sign_accuracy`、`mae`、`rmse` 和 `pearson`。

## 6. 启动服务并接入 Agent

终端一：

```powershell
picoclaw-qwen-delta-serve `
  --model .picoclaw/models/qwen-delta `
  --host 127.0.0.1 `
  --port 6007
```

终端二：

```powershell
picoclaw-real `
  --evidence `
  --evidence-selector http-direct `
  --selector-url http://127.0.0.1:6007 `
  "修复认证逻辑并运行测试"
```

## 7. 最终可对外报告的判定条件

必须在相同任务、相同 Agent/Reader、相同预算和外部 Verifier 下比较 `none`、`lexical`、`full`、`linear-trained`、`qwen-direct`。至少同时报告：任务通过率、Prompt Token、文件读取次数、工具调用数、Selector 延迟和失败降级次数。

只有真实验证集结果稳定优于基线后，才能把 Qwen 的提升数字写进简历。否则只能描述为“完成训练—服务—评测闭环”。
