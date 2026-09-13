"""Cross-strategy context benchmarks with isolated workspaces and verifiers."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .agent import Agent, AgentResult
from .context_manager import ContextBudget, ContextManager
from .selection import EvidenceProvider, RepositoryChunker
from .verifier import Verifier
from .workspace import Workspace

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


class BenchmarkTokenCounter(Protocol):
    mode: str

    def count_text(self, text: str) -> int: ...

    def truncate_text(self, text: str, limit: int) -> str: ...

    def count_messages(self, messages) -> int: ...


@dataclass(frozen=True, slots=True)
class ContextBenchmarkCase:
    name: str
    request: str
    verifier: Verifier
    fixture_files: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _SAFE_NAME.fullmatch(self.name):
            raise ValueError("context benchmark case name must be filesystem-safe")
        if not self.request.strip():
            raise ValueError("context benchmark request must not be empty")


EvidenceFactory = Callable[[Workspace, BenchmarkTokenCounter], EvidenceProvider | None]


@dataclass(frozen=True, slots=True)
class ContextStrategy:
    name: str
    evidence_factory: EvidenceFactory

    def __post_init__(self) -> None:
        if not _SAFE_NAME.fullmatch(self.name):
            raise ValueError("context strategy name must be filesystem-safe")


@dataclass(frozen=True, slots=True)
class ContextCaseResult:
    case: str
    strategy: str
    passed: bool
    status: str
    error_type: str | None
    model_steps: int
    tool_calls: int
    file_reads: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    repository_tokens: int
    evidence_tokens: int
    evidence_compression_ratio: float
    selected_chunks: int
    duplicate_rate: float
    rendered_context_tokens: int
    selector_latency_ms: int
    http_requests: int
    cache_hits: int
    cache_misses: int
    fallback_used: bool
    elapsed_ms: int
    workspace: str
    run_directory: str | None


@dataclass(frozen=True, slots=True)
class ContextStrategySummary:
    strategy: str
    cases: int
    passed: int
    pass_rate: float
    total_tool_calls: int
    total_file_reads: int
    total_prompt_tokens: int
    total_evidence_tokens: int
    average_compression_ratio: float
    average_selector_latency_ms: float
    total_http_requests: int
    total_cache_hits: int
    fallbacks: int


@dataclass(frozen=True, slots=True)
class ContextBenchmarkReport:
    suite_id: str
    results: tuple[ContextCaseResult, ...]
    strategies: tuple[ContextStrategySummary, ...]
    report_path: str


ContextAgentFactory = Callable[
    [ContextBenchmarkCase, ContextStrategy, Workspace, ContextManager, EvidenceProvider | None],
    Agent,
]
TokenCounterFactory = Callable[[], BenchmarkTokenCounter]


class ContextBenchmarkRunner:
    """Run every case/strategy pair in a fresh workspace."""

    def __init__(
        self,
        root: Path,
        agent_factory: ContextAgentFactory,
        token_counter_factory: TokenCounterFactory,
        context_budget: ContextBudget | None = None,
    ) -> None:
        self.root = root.resolve()
        self.agent_factory = agent_factory
        self.token_counter_factory = token_counter_factory
        self.context_budget = context_budget or ContextBudget()

    def run(
        self,
        cases: Sequence[ContextBenchmarkCase],
        strategies: Sequence[ContextStrategy],
    ) -> ContextBenchmarkReport:
        self._validate(cases, strategies)
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        suite_id = f"context-benchmark-{timestamp}-{uuid.uuid4().hex[:8]}"
        suite_root = self.root / suite_id
        suite_root.mkdir(parents=True, exist_ok=False)
        results: list[ContextCaseResult] = []

        for case in cases:
            for strategy in strategies:
                workspace = Workspace(suite_root / case.name / strategy.name / "workspace")
                workspace.root.mkdir(parents=True, exist_ok=False)
                for path, content in sorted(case.fixture_files.items()):
                    workspace.write_text(path, content)
                counter = self.token_counter_factory()
                repository_tokens = sum(
                    chunk.token_count
                    for chunk in RepositoryChunker(workspace, counter).chunk_repository()
                )
                evidence = strategy.evidence_factory(workspace, counter)
                context = ContextManager(counter, self.context_budget)
                agent = self.agent_factory(case, strategy, workspace, context, evidence)
                results.append(
                    self._run_case(
                        case,
                        strategy,
                        workspace,
                        agent,
                        repository_tokens,
                    )
                )

        report_path = suite_root / "context-benchmark-report.json"
        report = ContextBenchmarkReport(
            suite_id=suite_id,
            results=tuple(results),
            strategies=self._summaries(results, strategies),
            report_path=str(report_path),
        )
        temporary = report_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, report_path)
        return report

    @staticmethod
    def _run_case(
        case: ContextBenchmarkCase,
        strategy: ContextStrategy,
        workspace: Workspace,
        agent: Agent,
        repository_tokens: int,
    ) -> ContextCaseResult:
        started_at = time.perf_counter()
        result: AgentResult | None = None
        error_type: str | None = None
        try:
            result = agent.run(case.request)
            verification = case.verifier.verify(workspace, case.request)
            passed = verification.passed
            status = "passed" if passed else "verification_failed"
        except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
            passed = False
            status = "agent_failed"
            error_type = type(exc).__name__

        elapsed_ms = max(0, int((time.perf_counter() - started_at) * 1000))
        trace = result.trace if result is not None else ()
        selection = next(
            (event.metadata for event in trace if event.kind == "context_selection_finished"),
            {},
        )
        selected_events = [event for event in trace if event.kind == "context_chunk_selected"]
        content_hashes = [
            str(event.metadata.get("content_sha256", "")) for event in selected_events
        ]
        nonempty_hashes = [item for item in content_hashes if item]
        duplicate_rate = (
            1 - len(set(nonempty_hashes)) / len(nonempty_hashes) if nonempty_hashes else 0.0
        )
        context_tokens = [
            int(event.metadata.get("rendered_tokens", 0))
            for event in trace
            if event.kind == "context_built"
        ]
        evidence_tokens = int(selection.get("selected_tokens", 0))
        compression = (
            max(0.0, 1 - evidence_tokens / repository_tokens) if repository_tokens else 0.0
        )
        tool_events = [event for event in trace if event.kind == "tool_executed"]
        usage = result.usage if result is not None else None
        return ContextCaseResult(
            case=case.name,
            strategy=strategy.name,
            passed=passed,
            status=status,
            error_type=error_type,
            model_steps=result.steps if result is not None else 0,
            tool_calls=len(tool_events),
            file_reads=sum(event.metadata.get("name") == "read_file" for event in tool_events),
            prompt_tokens=usage.prompt_tokens if usage is not None else 0,
            completion_tokens=usage.completion_tokens if usage is not None else 0,
            total_tokens=usage.total_tokens if usage is not None else 0,
            repository_tokens=repository_tokens,
            evidence_tokens=evidence_tokens,
            evidence_compression_ratio=compression,
            selected_chunks=len(selected_events),
            duplicate_rate=duplicate_rate,
            rendered_context_tokens=max(context_tokens, default=0),
            selector_latency_ms=int(selection.get("elapsed_ms", 0)),
            http_requests=int(selection.get("http_requests", 0)),
            cache_hits=int(selection.get("cache_hits", 0)),
            cache_misses=int(selection.get("cache_misses", 0)),
            fallback_used=bool(selection.get("fallback_used", False)),
            elapsed_ms=elapsed_ms,
            workspace=str(workspace.root),
            run_directory=result.run_directory if result is not None else None,
        )

    @staticmethod
    def _summaries(
        results: Sequence[ContextCaseResult],
        strategies: Sequence[ContextStrategy],
    ) -> tuple[ContextStrategySummary, ...]:
        summaries: list[ContextStrategySummary] = []
        for strategy in strategies:
            items = [item for item in results if item.strategy == strategy.name]
            passed = sum(item.passed for item in items)
            summaries.append(
                ContextStrategySummary(
                    strategy=strategy.name,
                    cases=len(items),
                    passed=passed,
                    pass_rate=passed / len(items),
                    total_tool_calls=sum(item.tool_calls for item in items),
                    total_file_reads=sum(item.file_reads for item in items),
                    total_prompt_tokens=sum(item.prompt_tokens for item in items),
                    total_evidence_tokens=sum(item.evidence_tokens for item in items),
                    average_compression_ratio=sum(item.evidence_compression_ratio for item in items)
                    / len(items),
                    average_selector_latency_ms=sum(item.selector_latency_ms for item in items)
                    / len(items),
                    total_http_requests=sum(item.http_requests for item in items),
                    total_cache_hits=sum(item.cache_hits for item in items),
                    fallbacks=sum(item.fallback_used for item in items),
                )
            )
        return tuple(summaries)

    @staticmethod
    def _validate(
        cases: Sequence[ContextBenchmarkCase],
        strategies: Sequence[ContextStrategy],
    ) -> None:
        if not cases or not strategies:
            raise ValueError("context benchmark requires cases and strategies")
        case_names = [case.name for case in cases]
        strategy_names = [strategy.name for strategy in strategies]
        if len(case_names) != len(set(case_names)):
            raise ValueError("context benchmark case names must be unique")
        if len(strategy_names) != len(set(strategy_names)):
            raise ValueError("context strategy names must be unique")
