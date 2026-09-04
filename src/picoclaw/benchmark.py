"""Reproducible benchmark cases evaluated by external workspace verifiers."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .agent import Agent
from .checkpoint import CheckpointStore
from .goal_loop import GoalRunner
from .task_state import GoalBudget
from .verifier import Verifier
from .workspace import Workspace

_CASE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    name: str
    request: str
    verifier: Verifier
    fixture_files: dict[str, str] = field(default_factory=dict)
    budget: GoalBudget = field(default_factory=GoalBudget)

    def __post_init__(self) -> None:
        if not _CASE_NAME.fullmatch(self.name):
            raise ValueError("benchmark case name must be filesystem-safe")
        if not self.request.strip():
            raise ValueError("benchmark request must not be empty")


@dataclass(frozen=True, slots=True)
class BenchmarkCaseResult:
    name: str
    passed: bool
    status: str
    attempts: int
    model_steps: int
    tool_calls: int
    estimated_cost_usd: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    elapsed_seconds: float
    stopped_reason: str | None
    workspace: str
    report_path: str


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    suite_id: str
    cases: tuple[BenchmarkCaseResult, ...]
    passed: int
    failed: int
    pass_rate: float
    total_attempts: int
    total_model_steps: int
    total_tool_calls: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    total_estimated_cost_usd: float
    total_elapsed_seconds: float
    report_path: str


AgentFactory = Callable[[BenchmarkCase, Workspace], Agent]


class BenchmarkRunner:
    def __init__(self, root: Path, agent_factory: AgentFactory) -> None:
        self.root = root.resolve()
        self.agent_factory = agent_factory

    def run(self, cases: Sequence[BenchmarkCase]) -> BenchmarkReport:
        if not cases:
            raise ValueError("benchmark requires at least one case")
        names = [case.name for case in cases]
        if len(names) != len(set(names)):
            raise ValueError("benchmark case names must be unique")

        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        suite_id = f"benchmark-{timestamp}-{uuid.uuid4().hex[:8]}"
        suite_root = self.root / suite_id
        suite_root.mkdir(parents=True, exist_ok=False)
        results: list[BenchmarkCaseResult] = []

        for case in cases:
            workspace = Workspace(suite_root / case.name / "workspace")
            workspace.root.mkdir(parents=True, exist_ok=False)
            for path, content in sorted(case.fixture_files.items()):
                workspace.write_text(path, content)
            agent = self.agent_factory(case, workspace)
            checkpoint_store = CheckpointStore(workspace.root / ".picoclaw" / "checkpoints")
            goal = GoalRunner(
                agent,
                workspace,
                case.verifier,
                checkpoint_store,
                budget=case.budget,
            ).run(case.request)
            state = goal.state
            results.append(
                BenchmarkCaseResult(
                    name=case.name,
                    passed=goal.succeeded,
                    status=state.status.value,
                    attempts=state.attempts,
                    model_steps=state.model_steps,
                    tool_calls=state.tool_calls,
                    estimated_cost_usd=state.estimated_cost_usd,
                    prompt_tokens=state.prompt_tokens,
                    completion_tokens=state.completion_tokens,
                    total_tokens=state.total_tokens,
                    elapsed_seconds=state.elapsed_seconds,
                    stopped_reason=state.stopped_reason,
                    workspace=str(workspace.root),
                    report_path=str(goal.report_path),
                )
            )

        passed = sum(item.passed for item in results)
        report_path = suite_root / "benchmark-report.json"
        report = BenchmarkReport(
            suite_id=suite_id,
            cases=tuple(results),
            passed=passed,
            failed=len(results) - passed,
            pass_rate=passed / len(results),
            total_attempts=sum(item.attempts for item in results),
            total_model_steps=sum(item.model_steps for item in results),
            total_tool_calls=sum(item.tool_calls for item in results),
            total_prompt_tokens=sum(item.prompt_tokens for item in results),
            total_completion_tokens=sum(item.completion_tokens for item in results),
            total_tokens=sum(item.total_tokens for item in results),
            total_estimated_cost_usd=sum(item.estimated_cost_usd for item in results),
            total_elapsed_seconds=sum(item.elapsed_seconds for item in results),
            report_path=str(report_path),
        )
        temporary = report_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, report_path)
        return report
