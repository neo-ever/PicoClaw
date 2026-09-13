"""Offline M7-C demo: aligned data, training, and cross-strategy benchmark."""

from __future__ import annotations

import argparse
import json
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .agent import Agent
from .context_benchmark import (
    ContextBenchmarkCase,
    ContextBenchmarkRunner,
    ContextStrategy,
)
from .context_manager import ContextBudget
from .models import Message, ModelResponse, TokenUsage, ToolCall
from .run_store import RunStore
from .selection import (
    ContextChunk,
    DeltaDatasetBuilder,
    FullContextSelector,
    HttpDeltaSelector,
    HttpSelectorConfig,
    LexicalSelector,
    LinearDeltaSelector,
    RepositoryEvidenceProvider,
    evaluate_linear_delta,
    split_delta_examples,
    train_linear_delta,
    write_delta_jsonl,
)
from .tools import build_coding_registry
from .verifier import FileContentVerifier


class DemoTokenCounter:
    mode = "demo:four-chars-per-token"

    @staticmethod
    def count_text(text: str) -> int:
        return (len(text) + 3) // 4

    @staticmethod
    def truncate_text(text: str, limit: int) -> str:
        return text[: limit * 4]

    def count_messages(self, messages) -> int:
        return sum(self.count_text(message.content) + 4 for message in messages)


class SyntheticOutcomeScorer:
    """Deterministic labeler used only for the offline pipeline proof."""

    def score(self, query: str, context: str) -> float:
        target = re.search(r"target_\d+", query)
        if target and target.group() in context:
            return 1.0
        return 0.0 if not context else -1.0


class SimulatedQuestionLogprobTransport:
    """Simulate the legacy 0.8B HTTP contract for controlled A/B plumbing."""

    def post_json(self, url, payload, timeout_seconds):
        del url, timeout_seconds
        return {
            "scores": [
                {"score": 10.0 if "BUG_" in item["context"] else 0.0} for item in payload["items"]
            ]
        }


@dataclass(frozen=True, slots=True)
class RepairSpec:
    name: str
    request: str
    path: str
    marker: str
    original: str
    fixed: str


class RepairProvider:
    """Fixed Reader policy: use Evidence, otherwise read the target, then patch."""

    def __init__(self, spec: RepairSpec) -> None:
        self.spec = spec

    def complete(self, messages: list[Message], tools: list[dict]) -> ModelResponse:
        del tools
        visible = "\n".join(message.content for message in messages)
        prompt_tokens = max(1, len(visible) // 4)
        usage = TokenUsage(prompt_tokens, 4, prompt_tokens + 4)
        tool_messages = [message for message in messages if message.role == "tool"]
        if tool_messages and tool_messages[-1].name == "write_file":
            return ModelResponse(content="Patch completed for external verification.", usage=usage)
        if self.spec.marker not in visible:
            return ModelResponse(
                tool_calls=(ToolCall("read-target", "read_file", {"path": self.spec.path}),),
                usage=usage,
            )
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    "write-target",
                    "write_file",
                    {"path": self.spec.path, "content": self.spec.fixed},
                ),
            ),
            usage=usage,
        )


def build_synthetic_dataset(output: Path, task_count: int = 12):
    if task_count < 2:
        raise ValueError("synthetic dataset requires at least two tasks")
    builder = DeltaDatasetBuilder(SyntheticOutcomeScorer(), "synthetic-code-fixture")
    examples = []
    for index in range(task_count):
        candidates = [
            ContextChunk(
                id=f"relevant-{index}",
                path=f"src/target_{index}.py",
                start_line=1,
                end_line=1,
                content=f"def target_{index}(): return 'BUG_TARGET'",
                source_sha256=f"sha-relevant-{index}",
                token_count=8,
            ),
            ContextChunk(
                id=f"noise-{index}",
                path=f"src/noise_{index}.py",
                start_line=1,
                end_line=1,
                content=f"def unrelated_{index}(): return 'noise'",
                source_sha256=f"sha-noise-{index}",
                token_count=8,
            ),
        ]
        examples.extend(
            builder.build_task(
                f"task-{index}",
                f"Find and repair target_{index}",
                candidates,
            )
        )
    train, validation = split_delta_examples(examples, validation_ratio=0.25, seed=42)
    data_root = output / "data"
    train_path = write_delta_jsonl(data_root / "train.jsonl", train)
    validation_path = write_delta_jsonl(data_root / "validation.jsonl", validation)
    return train, validation, train_path, validation_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the offline M7-C pipeline demo")
    parser.add_argument(
        "--task-count",
        type=int,
        default=12,
        help="Number of synthetic tasks; each creates one positive and one negative pair",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.task_count < 2:
        raise SystemExit("--task-count must be at least 2")
    demo_root = Path.cwd() / ".picoclaw" / f"m7c-demo-{uuid.uuid4().hex[:8]}"
    demo_root.mkdir(parents=True, exist_ok=False)
    train, validation, train_path, validation_path = build_synthetic_dataset(
        demo_root,
        task_count=args.task_count,
    )
    linear_model = train_linear_delta(train)
    train_metrics = evaluate_linear_delta(linear_model, train)
    validation_metrics = evaluate_linear_delta(linear_model, validation)
    model_path = linear_model.save(demo_root / "models" / "linear-delta.json")
    training_report_path = demo_root / "linear-training-report.json"
    training_report_path.write_text(
        json.dumps(
            {
                "scope": "synthetic pipeline proof; not a Qwen or real-task result",
                "train": asdict(train_metrics),
                "validation": asdict(validation_metrics),
                "model_path": str(model_path),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    specs = [
        RepairSpec(
            "refresh-token",
            "Fix validate_refresh_token expiration logic",
            "src/auth.py",
            "BUG_AUTH",
            "def validate_refresh_token(token):\n    return 'BUG_AUTH'\n",
            "def validate_refresh_token(token):\n    return token.is_valid\n",
        ),
        RepairSpec(
            "cache-key",
            "Fix build_cache_key namespace handling",
            "src/cache.py",
            "BUG_CACHE",
            "def build_cache_key(name):\n    return 'BUG_CACHE'\n",
            "def build_cache_key(name):\n    return f'picoclaw:{name}'\n",
        ),
        RepairSpec(
            "config-parser",
            "Fix parse_timeout integer conversion",
            "src/config.py",
            "BUG_CONFIG",
            "def parse_timeout(value):\n    return 'BUG_CONFIG'\n",
            "def parse_timeout(value):\n    return int(value)\n",
        ),
    ]
    spec_by_name = {spec.name: spec for spec in specs}
    cases = [
        ContextBenchmarkCase(
            spec.name,
            spec.request,
            FileContentVerifier(spec.path, spec.fixed),
            fixture_files={
                spec.path: spec.original,
                "docs/overview.md": "PicoClaw is a bounded coding agent runtime.\n",
                f"src/noise_{index}.py": f"def unrelated_{index}():\n    return {index}\n",
            },
        )
        for index, spec in enumerate(specs)
    ]

    def evidence(selector):
        def factory(workspace, counter):
            return RepositoryEvidenceProvider(
                workspace,
                counter,
                selector=selector(),
                token_budget=320,
            )

        return factory

    def no_evidence(workspace, counter):
        del workspace, counter

    strategies = [
        ContextStrategy("none", no_evidence),
        ContextStrategy("lexical", evidence(LexicalSelector)),
        ContextStrategy(
            "http-simulated",
            evidence(
                lambda: HttpDeltaSelector(
                    HttpSelectorConfig(candidate_pool_size=8),
                    transport=SimulatedQuestionLogprobTransport(),
                )
            ),
        ),
        ContextStrategy("full", evidence(FullContextSelector)),
        ContextStrategy(
            "linear-trained",
            evidence(lambda: LinearDeltaSelector(linear_model)),
        ),
    ]

    def agent_factory(case, strategy, workspace, context, selected_evidence):
        del strategy
        return Agent(
            RepairProvider(spec_by_name[case.name]),
            build_coding_registry(workspace, approval_mode="auto"),
            max_steps=4,
            run_store=RunStore(workspace.root / ".picoclaw" / "runs"),
            context_manager=context,
            evidence_provider=selected_evidence,
        )

    benchmark = ContextBenchmarkRunner(
        demo_root / "benchmarks",
        agent_factory,
        DemoTokenCounter,
        ContextBudget(
            max_input_tokens=1_200,
            max_memory_tokens=160,
            max_summary_tokens=80,
            max_evidence_tokens=320,
        ),
    ).run(cases, strategies)

    print("=== M7-C Data / Training / A-B Demo ===")
    print(
        f"dataset=train:{len(train)}, validation:{len(validation)}, "
        f"task_overlap={bool({item.task_id for item in train} & {item.task_id for item in validation})}"
    )
    print(
        "linear_validation="
        f"sign_accuracy:{validation_metrics.sign_accuracy:.1%}, "
        f"mae:{validation_metrics.mae:.4f}, pearson:{validation_metrics.pearson:.4f}"
    )
    print("strategy | pass_rate | file_reads | evidence_tokens | http_requests")
    for summary in benchmark.strategies:
        print(
            f"{summary.strategy} | {summary.pass_rate:.0%} | "
            f"{summary.total_file_reads} | {summary.total_evidence_tokens} | "
            f"{summary.total_http_requests}"
        )
    print(f"train_data={train_path}")
    print(f"validation_data={validation_path}")
    print(f"training_report={training_report_path}")
    print(f"benchmark_report={benchmark.report_path}")


if __name__ == "__main__":
    main()
