import json
from pathlib import Path

import pytest

from picoclaw import (
    Agent,
    AgentBudgetExceeded,
    AgentRunBudget,
    BenchmarkCase,
    BenchmarkRunner,
    CheckpointStaleError,
    CheckpointStore,
    FileContentVerifier,
    GoalBudget,
    GoalRunner,
    ModelResponse,
    TaskStatus,
    TokenPricing,
    TokenUsage,
    ToolCall,
    Workspace,
)
from picoclaw.tools import build_coding_registry, build_read_only_registry


class ClaimOnlyProvider:
    def complete(self, messages, tools):
        del messages, tools
        return ModelResponse(content="I finished the task.")


class CorrectionProvider:
    """Make a wrong first attempt, then use verifier feedback to correct it."""

    def complete(self, messages, tools):
        del tools
        tool_results = [message for message in messages if message.role == "tool"]
        if tool_results:
            return ModelResponse(
                content="Configuration written.",
                usage=TokenUsage(5, 2, 7),
            )
        prompt = next(message.content for message in messages if message.role == "user")
        content = "mode=prod\n" if "Independent verifier feedback" in prompt else "mode=dev\n"
        return ModelResponse(
            tool_calls=(
                ToolCall("write-config", "write_file", {"path": "app.cfg", "content": content}),
            ),
            usage=TokenUsage(5, 3, 8),
        )


def goal_runner(
    workspace: Workspace,
    provider,
    budget: GoalBudget,
) -> GoalRunner:
    agent = Agent(
        provider,
        build_coding_registry(workspace, approval_mode="auto"),
        max_steps=3,
    )
    return GoalRunner(
        agent,
        workspace,
        FileContentVerifier("app.cfg", "mode=prod\n"),
        CheckpointStore(workspace.root / ".picoclaw" / "checkpoints"),
        budget=budget,
    )


def test_agent_tool_budget_stops_before_tool_execution(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("secret", encoding="utf-8")

    class ReadProvider:
        def complete(self, messages, tools):
            del messages, tools
            return ModelResponse(
                tool_calls=(ToolCall("read-1", "read_file", {"path": "data.txt"}),)
            )

    agent = Agent(ReadProvider(), build_read_only_registry(Workspace(tmp_path)))

    with pytest.raises(AgentBudgetExceeded) as captured:
        agent.run(
            "read data",
            AgentRunBudget(
                max_model_steps=2,
                max_tool_calls=0,
                max_total_tokens=100,
                max_seconds=10,
            ),
        )

    assert captured.value.reason == "tool_calls"
    assert captured.value.tool_calls == 0


def test_verifier_rejects_an_unsubstantiated_model_claim(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    runner = goal_runner(
        workspace,
        ClaimOnlyProvider(),
        GoalBudget(max_attempts=1),
    )

    result = runner.run("Create app.cfg with production mode")

    assert result.state.status is TaskStatus.BUDGET_EXHAUSTED
    assert result.state.history[0].verification_passed is False
    assert not (tmp_path / "app.cfg").exists()


def test_goal_loop_uses_verifier_feedback_for_a_second_attempt(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    result = goal_runner(workspace, CorrectionProvider(), GoalBudget()).run(
        "Create app.cfg with production mode"
    )

    assert result.succeeded is True
    assert result.state.attempts == 2
    assert result.state.model_steps == 4
    assert result.state.tool_calls == 2
    assert result.state.total_tokens == 30
    assert (tmp_path / "app.cfg").read_text(encoding="utf-8") == "mode=prod\n"
    assert "expected exact content" in result.state.history[0].verification_summary


def test_checkpoint_can_resume_but_rejects_external_workspace_change(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    request = "Create app.cfg with production mode"
    first = goal_runner(
        workspace,
        CorrectionProvider(),
        GoalBudget(max_attempts=1),
    ).run(request)
    assert first.state.status is TaskStatus.BUDGET_EXHAUSTED

    resumed = goal_runner(
        workspace,
        CorrectionProvider(),
        GoalBudget(max_attempts=2),
    ).run(request, resume=True)
    assert resumed.succeeded is True
    assert resumed.state.attempts == 2

    (tmp_path / "app.cfg").write_text("changed outside runtime\n", encoding="utf-8")
    with pytest.raises(CheckpointStaleError, match="workspace files changed"):
        goal_runner(
            workspace,
            CorrectionProvider(),
            GoalBudget(max_attempts=3),
        ).run(request, resume=True)


def test_checkpoint_rejects_changed_runtime_identity(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    request = "Create app.cfg with production mode"
    runner = goal_runner(workspace, CorrectionProvider(), GoalBudget(max_attempts=1))
    runner.run(request)
    changed_agent = Agent(
        CorrectionProvider(),
        build_coding_registry(workspace, approval_mode="never"),
        max_steps=3,
    )

    with pytest.raises(CheckpointStaleError, match="runtime identity changed"):
        GoalRunner(
            changed_agent,
            workspace,
            runner.verifier,
            runner.checkpoint_store,
            budget=GoalBudget(max_attempts=2),
        ).run(request, resume=True)


def test_goal_loop_stops_after_repeated_no_progress(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    result = goal_runner(
        workspace,
        ClaimOnlyProvider(),
        GoalBudget(max_attempts=5, max_stagnant_attempts=2),
    ).run("Create app.cfg with production mode")

    assert result.state.status is TaskStatus.STALLED
    assert result.state.stopped_reason == "stagnation"
    assert result.state.attempts == 2


def test_goal_loop_accounts_for_and_stops_on_estimated_cost(tmp_path: Path) -> None:
    class MeteredProvider:
        def complete(self, messages, tools):
            del messages, tools
            return ModelResponse(
                content="I finished the task.",
                usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            )

    workspace = Workspace(tmp_path)
    agent = Agent(MeteredProvider(), build_read_only_registry(workspace))
    result = GoalRunner(
        agent,
        workspace,
        FileContentVerifier("missing.txt", "done"),
        CheckpointStore(workspace.root / ".picoclaw" / "checkpoints"),
        budget=GoalBudget(max_attempts=5, max_cost_usd=0.1),
        pricing=TokenPricing(input_per_million_usd=1_000, output_per_million_usd=1_000),
    ).run("Create missing.txt")

    assert result.state.status is TaskStatus.BUDGET_EXHAUSTED
    assert result.state.stopped_reason == "cost"
    assert result.state.estimated_cost_usd == pytest.approx(0.15)
    assert result.state.attempts == 1


def test_benchmark_aggregates_external_verification_results(tmp_path: Path) -> None:
    def factory(case, workspace):
        del case
        return Agent(ClaimOnlyProvider(), build_read_only_registry(workspace))

    cases = [
        BenchmarkCase(
            "passing-case",
            "Confirm ready.txt",
            FileContentVerifier("ready.txt", "yes\n"),
            fixture_files={"ready.txt": "yes\n"},
            budget=GoalBudget(max_attempts=1),
        ),
        BenchmarkCase(
            "failing-case",
            "Create missing.txt",
            FileContentVerifier("missing.txt", "created\n"),
            budget=GoalBudget(max_attempts=1),
        ),
    ]

    report = BenchmarkRunner(tmp_path / "benchmarks", factory).run(cases)
    persisted = json.loads(Path(report.report_path).read_text(encoding="utf-8"))

    assert report.passed == 1
    assert report.failed == 1
    assert report.pass_rate == 0.5
    assert persisted["pass_rate"] == 0.5
