import json
import re
from pathlib import Path

import pytest

from picoclaw import (
    Agent,
    ContextBenchmarkCase,
    ContextBenchmarkRunner,
    ContextChunk,
    ContextStrategy,
    DeltaDatasetBuilder,
    DeltaExample,
    DirectDeltaHttpSelector,
    FileContentVerifier,
    FullContextSelector,
    HttpSelectorConfig,
    LinearDeltaModel,
    ModelResponse,
    RepositoryEvidenceProvider,
    RunStore,
    TokenUsage,
    ToolCall,
    evaluate_linear_delta,
    read_delta_jsonl,
    split_delta_examples,
    train_linear_delta,
    write_delta_jsonl,
)
from picoclaw.qwen_delta_service import validate_items
from picoclaw.qwen_preflight import audit_datasets
from picoclaw.tools import build_coding_registry


class SimpleCounter:
    mode = "test:words"

    @staticmethod
    def count_text(text):
        return max(1, len(text.split())) if text else 0

    @staticmethod
    def truncate_text(text, limit):
        return " ".join(text.split()[:limit])

    def count_messages(self, messages):
        return sum(self.count_text(message.content) + 4 for message in messages)


def make_chunk(chunk_id: str, content: str, path: str | None = None) -> ContextChunk:
    return ContextChunk(
        id=chunk_id,
        path=path or f"src/{chunk_id}.py",
        start_line=1,
        end_line=1,
        content=content,
        source_sha256=f"sha-{chunk_id}",
        token_count=max(1, len(content.split())),
    )


class QueryTargetScorer:
    def score(self, query: str, context: str) -> float:
        target = re.search(r"target_\d+", query)
        if target and target.group() in context:
            return 1.0
        return 0.0 if not context else -1.0


def build_examples(task_count: int = 8) -> tuple[DeltaExample, ...]:
    builder = DeltaDatasetBuilder(QueryTargetScorer(), source="synthetic-code-fixture")
    examples = []
    for index in range(task_count):
        examples.extend(
            builder.build_task(
                f"task-{index}",
                f"Find and repair target_{index}",
                [
                    make_chunk(
                        f"relevant-{index}",
                        f"def target_{index}(): return 'BUG_MARKER'",
                    ),
                    make_chunk(
                        f"noise-{index}",
                        f"def unrelated_{index}(): return 'noise'",
                    ),
                ],
            )
        )
    return tuple(examples)


def test_delta_dataset_round_trip_and_grouped_split(tmp_path: Path) -> None:
    examples = build_examples()
    path = write_delta_jsonl(tmp_path / "delta.jsonl", examples)
    loaded = read_delta_jsonl(path)
    train, validation = split_delta_examples(loaded, validation_ratio=0.25, seed=7)

    assert loaded == examples
    assert {example.label for example in loaded} == {0, 1}
    assert {example.task_id for example in train}.isdisjoint(
        example.task_id for example in validation
    )
    assert len(train) + len(validation) == len(examples)


def test_qwen_preflight_audits_task_isolation_and_labels(tmp_path: Path) -> None:
    train, validation = split_delta_examples(build_examples(), validation_ratio=0.25)
    train_path = write_delta_jsonl(tmp_path / "train.jsonl", train)
    validation_path = write_delta_jsonl(tmp_path / "validation.jsonl", validation)

    audit = audit_datasets(train_path, validation_path)

    assert audit.train_examples == len(train)
    assert audit.validation_examples == len(validation)
    assert audit.overlapping_tasks == ()
    assert 0 < audit.train_positive < audit.train_examples


def test_delta_example_rejects_misaligned_target() -> None:
    with pytest.raises(ValueError, match="delta must equal"):
        DeltaExample(
            example_id="bad",
            task_id="task",
            query="query",
            minus_context="",
            candidate_context="candidate",
            plus_context="candidate",
            score_minus=0.0,
            score_plus=1.0,
            delta=0.5,
            label=1,
            source="test",
        )


def test_linear_delta_training_evaluation_and_persistence(tmp_path: Path) -> None:
    train, validation = split_delta_examples(build_examples(), validation_ratio=0.25)
    model = train_linear_delta(train)
    metrics = evaluate_linear_delta(model, validation)
    path = model.save(tmp_path / "linear-delta.json")
    reloaded = LinearDeltaModel.load(path)
    example = validation[0]

    assert metrics.sign_accuracy >= 0.75
    assert metrics.mae < 0.6
    assert reloaded.predict(
        example.query,
        example.minus_context,
        example.candidate_context,
    ) == pytest.approx(
        model.predict(example.query, example.minus_context, example.candidate_context)
    )


def test_direct_delta_http_selector_uses_aligned_batch_contract() -> None:
    class DirectTransport:
        def __init__(self):
            self.urls = []
            self.payloads = []

        def post_json(self, url, payload, timeout_seconds):
            del timeout_seconds
            self.urls.append(url)
            self.payloads.append(payload)
            return {
                "deltas": [
                    {
                        "delta": (
                            2.0 if "validate_refresh_token" in item["candidate_context"] else -1.0
                        )
                    }
                    for item in payload["items"]
                ]
            }

    transport = DirectTransport()
    selector = DirectDeltaHttpSelector(
        HttpSelectorConfig(batch_size=8),
        transport=transport,
    )
    candidates = [
        make_chunk("auth", "def validate_refresh_token(token): return token.is_valid"),
        make_chunk("docs", "refresh token documentation"),
    ]

    result = selector.select("Explain validate_refresh_token", candidates, token_budget=20)

    assert result.fallback_used is False
    assert [item.chunk.id for item in result.selected] == ["auth"]
    assert transport.urls[0].endswith("/predict_delta_batch")
    assert set(transport.payloads[0]["items"][0]) == {
        "candidate_context",
        "minus_context",
        "plus_context",
        "question",
    }


def test_qwen_service_validates_the_same_direct_delta_fields() -> None:
    items = validate_items(
        {
            "items": [
                {
                    "question": "Find auth",
                    "minus_context": "",
                    "candidate_context": "def auth(): pass",
                    "plus_context": "def auth(): pass",
                }
            ]
        },
        max_batch_size=4,
    )

    assert items[0]["candidate_context"] == "def auth(): pass"
    with pytest.raises(ValueError, match="batch size"):
        validate_items({"items": []}, max_batch_size=4)


class PatchProvider:
    def complete(self, messages, tools):
        del tools
        visible = "\n".join(message.content for message in messages)
        usage = TokenUsage(
            prompt_tokens=max(1, len(visible) // 4),
            completion_tokens=4,
            total_tokens=max(1, len(visible) // 4) + 4,
        )
        tool_messages = [message for message in messages if message.role == "tool"]
        if tool_messages and tool_messages[-1].name == "write_file":
            return ModelResponse(content="Patch complete.", usage=usage)
        if "BUG_MARKER" not in visible:
            return ModelResponse(
                tool_calls=(ToolCall("read-target", "read_file", {"path": "app.py"}),),
                usage=usage,
            )
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    "write-target",
                    "write_file",
                    {"path": "app.py", "content": "def target():\n    return 'fixed'\n"},
                ),
            ),
            usage=usage,
        )


def test_context_benchmark_compares_isolated_strategies(tmp_path: Path) -> None:
    def agent_factory(case, strategy, workspace, context, evidence):
        del case, strategy
        return Agent(
            PatchProvider(),
            build_coding_registry(workspace, approval_mode="auto"),
            max_steps=4,
            run_store=RunStore(workspace.root / ".picoclaw" / "runs"),
            context_manager=context,
            evidence_provider=evidence,
        )

    def no_evidence(workspace, counter):
        del workspace, counter

    def full_evidence(workspace, counter):
        return RepositoryEvidenceProvider(
            workspace,
            counter,
            selector=FullContextSelector(),
            token_budget=200,
        )

    report = ContextBenchmarkRunner(
        tmp_path / "benchmarks",
        agent_factory,
        SimpleCounter,
    ).run(
        [
            ContextBenchmarkCase(
                "fix-target",
                "Fix the target function",
                FileContentVerifier("app.py", "def target():\n    return 'fixed'\n"),
                fixture_files={
                    "app.py": "def target():\n    return 'BUG_MARKER'\n",
                    "noise.py": "def unrelated():\n    return 0\n",
                },
            )
        ],
        [
            ContextStrategy("none", no_evidence),
            ContextStrategy("full", full_evidence),
        ],
    )

    by_strategy = {item.strategy: item for item in report.results}
    assert all(item.passed for item in report.results)
    assert by_strategy["none"].file_reads == 1
    assert by_strategy["full"].file_reads == 0
    assert by_strategy["full"].selected_chunks == 2
    assert Path(report.report_path).is_file()
    payload = json.loads(Path(report.report_path).read_text(encoding="utf-8"))
    assert len(payload["strategies"]) == 2
