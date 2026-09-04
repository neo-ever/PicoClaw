"""Offline M6 demo: interruption, checkpoint resume, verification, and benchmark."""

from __future__ import annotations

import uuid
from pathlib import Path

from .agent import Agent
from .benchmark import BenchmarkCase, BenchmarkRunner
from .checkpoint import CheckpointStore
from .goal_loop import GoalRunner
from .models import Message, ModelResponse, TokenUsage, ToolCall
from .task_state import GoalBudget
from .tools import build_coding_registry, build_read_only_registry
from .verifier import FileContentVerifier
from .workspace import Workspace


class CorrectionDemoProvider:
    """Write a bad value first; use verifier feedback after resume."""

    def complete(self, messages: list[Message], tools: list[dict]) -> ModelResponse:
        del tools
        tool_results = [message for message in messages if message.role == "tool"]
        if tool_results:
            return ModelResponse(
                content="Configuration written.",
                usage=TokenUsage(8, 3, 11),
            )
        prompt = next(message.content for message in messages if message.role == "user")
        value = "mode=prod\n" if "Independent verifier feedback" in prompt else "mode=dev\n"
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    "write-app-config",
                    "write_file",
                    {"path": "app.cfg", "content": value},
                ),
            ),
            usage=TokenUsage(10, 4, 14),
        )


class ClaimOnlyDemoProvider:
    def complete(self, messages: list[Message], tools: list[dict]) -> ModelResponse:
        del messages, tools
        return ModelResponse(
            content="The requested file is ready.",
            usage=TokenUsage(4, 3, 7),
        )


def build_goal(workspace: Workspace, max_attempts: int) -> GoalRunner:
    agent = Agent(
        CorrectionDemoProvider(),
        build_coding_registry(workspace, approval_mode="auto"),
        max_steps=3,
    )
    return GoalRunner(
        agent,
        workspace,
        FileContentVerifier("app.cfg", "mode=prod\n"),
        CheckpointStore(workspace.root / ".picoclaw" / "checkpoints"),
        budget=GoalBudget(max_attempts=max_attempts),
    )


def main() -> None:
    demo_root = Path.cwd() / ".picoclaw" / f"m6-demo-{uuid.uuid4().hex[:8]}"
    workspace = Workspace(demo_root / "goal-workspace")
    workspace.root.mkdir(parents=True, exist_ok=False)
    request = "Create app.cfg and set mode=prod"

    interrupted = build_goal(workspace, max_attempts=1).run(request)
    resumed = build_goal(workspace, max_attempts=2).run(request, resume=True)

    def agent_factory(case: BenchmarkCase, case_workspace: Workspace) -> Agent:
        del case
        return Agent(ClaimOnlyDemoProvider(), build_read_only_registry(case_workspace))

    benchmark = BenchmarkRunner(demo_root / "benchmarks", agent_factory).run(
        [
            BenchmarkCase(
                "verified-existing-file",
                "Confirm ready.txt exists",
                FileContentVerifier("ready.txt", "ready\n"),
                fixture_files={"ready.txt": "ready\n"},
                budget=GoalBudget(max_attempts=1),
            ),
            BenchmarkCase(
                "reject-empty-claim",
                "Create required.txt",
                FileContentVerifier("required.txt", "created\n"),
                budget=GoalBudget(max_attempts=1),
            ),
        ]
    )

    print("=== M6 Goal Loop Demo ===")
    print(
        f"1. First run: status={interrupted.state.status.value}, "
        f"attempts={interrupted.state.attempts}, checkpoint={interrupted.checkpoint_path}"
    )
    print(
        f"2. Resume: status={resumed.state.status.value}, "
        f"attempts={resumed.state.attempts}, content={workspace.read_text('app.cfg').strip()!r}"
    )
    print(
        f"3. Benchmark: passed={benchmark.passed}, failed={benchmark.failed}, "
        f"pass_rate={benchmark.pass_rate:.0%}"
    )
    print(f"Benchmark report: {benchmark.report_path}")


if __name__ == "__main__":
    main()
