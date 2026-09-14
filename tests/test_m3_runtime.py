import json
from pathlib import Path

import pytest

from picoclaw import Agent, ModelResponse, RunStore, ToolCall, Workspace
from picoclaw.tools import build_coding_registry


class ScriptedProvider:
    def __init__(self, responses):
        self.responses = iter(responses)

    def complete(self, messages, tools):
        return next(self.responses)


def test_never_policy_rejects_write_without_touching_file(tmp_path: Path) -> None:
    registry = build_coding_registry(tmp_workspace(tmp_path), approval_mode="never")

    result = registry.execute(
        ToolCall("write-1", "write_file", {"path": "created.txt", "content": "hello"})
    )

    assert result.is_error is True
    assert result.metadata["status"] == "rejected"
    assert not (tmp_path / "created.txt").exists()


def test_auto_policy_approves_atomic_workspace_write(tmp_path: Path) -> None:
    registry = build_coding_registry(tmp_workspace(tmp_path), approval_mode="auto")

    result = registry.execute(
        ToolCall("write-1", "write_file", {"path": "nested/created.txt", "content": "hello"})
    )

    assert result.is_error is False
    assert result.metadata["risk"] == "write"
    assert result.metadata["approved"] is True
    assert (tmp_path / "nested" / "created.txt").read_text(encoding="utf-8") == "hello"


def test_ask_policy_redacts_content_before_prompting(tmp_path: Path) -> None:
    questions = []

    def approve(question: str) -> str:
        questions.append(question)
        return "yes"

    registry = build_coding_registry(
        tmp_workspace(tmp_path), approval_mode="ask", approval_prompt=approve
    )

    result = registry.execute(
        ToolCall("write-1", "write_file", {"path": "created.txt", "content": "private-text"})
    )

    assert result.is_error is False
    assert "private-text" not in questions[0]
    assert "<redacted 12 bytes>" in questions[0]


def test_write_tool_cannot_escape_workspace(tmp_path: Path) -> None:
    registry = build_coding_registry(tmp_workspace(tmp_path), approval_mode="auto")

    result = registry.execute(
        ToolCall("write-1", "write_file", {"path": "../outside.txt", "content": "blocked"})
    )

    assert result.is_error is True
    assert "path escapes workspace" in result.content


@pytest.mark.parametrize(
    "command",
    [
        "python -c print('unsafe')",
        "git reset --hard",
        "pytest; echo unsafe",
        "../python -m pytest",
    ],
)
def test_shell_rejects_commands_outside_allowlist(tmp_path: Path, command: str) -> None:
    workspace = tmp_workspace(tmp_path)

    with pytest.raises(ValueError):
        workspace.run_shell(command)


def test_shell_runs_allowlisted_command_without_shell_interpreter(tmp_path: Path) -> None:
    result = tmp_workspace(tmp_path).run_shell("python -m pytest --version")

    assert "exit_code=0" in result
    assert "pytest" in result


def test_read_only_discovery_tools_are_bounded_and_skip_generated_files(tmp_path: Path) -> None:
    source = tmp_path / "src" / "agent.py"
    source.parent.mkdir()
    source.write_text("first\nclass AgentLoop:\nthird\n", encoding="utf-8")
    dependency = tmp_path / ".venv-autodl" / "lib"
    dependency.mkdir(parents=True)
    (dependency / "noise.py").write_text("class AgentLoop: pass\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=AgentLoop\n", encoding="utf-8")
    registry = build_coding_registry(tmp_workspace(tmp_path), approval_mode="never")

    listed = registry.execute(ToolCall("list-1", "list_files", {"pattern": "*.py"}))
    searched = registry.execute(
        ToolCall("search-1", "search_text", {"query": "AgentLoop", "file_pattern": "*.py"})
    )
    ranged = registry.execute(
        ToolCall(
            "read-1",
            "read_file",
            {"path": "src/agent.py", "start_line": 2, "end_line": 2},
        )
    )

    assert listed.is_error is False
    assert listed.content == "src/agent.py"
    assert searched.is_error is False
    assert searched.content == "src/agent.py:2: class AgentLoop:"
    assert ranged.is_error is False
    assert ranged.content == "class AgentLoop:\n"
    assert all(result.metadata["risk"] == "read_only" for result in (listed, searched, ranged))


def test_search_text_cannot_escape_workspace(tmp_path: Path) -> None:
    registry = build_coding_registry(tmp_workspace(tmp_path), approval_mode="never")

    result = registry.execute(
        ToolCall("search-1", "search_text", {"query": "secret", "path": ".."})
    )

    assert result.is_error is True
    assert "path escapes workspace" in result.content


def test_agent_persists_trace_and_report_for_successful_write(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        "write-1",
                        "write_file",
                        {"path": "generated.txt", "content": "created by agent"},
                    ),
                )
            ),
            ModelResponse(content="The file was created."),
        ]
    )
    workspace = tmp_workspace(tmp_path)
    run_store = RunStore(tmp_path / ".picoclaw" / "runs")
    agent = Agent(
        provider,
        build_coding_registry(workspace, approval_mode="auto"),
        run_store=run_store,
    )

    result = agent.run("Create generated.txt")

    assert result.run_directory is not None
    run_directory = Path(result.run_directory)
    trace_lines = [
        json.loads(line) for line in (run_directory / "trace.jsonl").read_text().splitlines()
    ]
    report = json.loads((run_directory / "report.json").read_text(encoding="utf-8"))
    assert [event["kind"] for event in trace_lines] == [
        "run_started",
        "model_responded",
        "tool_executed",
        "model_responded",
        "run_finished",
    ]
    assert trace_lines[2]["metadata"]["risk"] == "write"
    assert trace_lines[2]["metadata"]["approved"] is True
    assert report["status"] == "success"
    assert report["tool_calls"] == 1
    assert report["tool_errors"] == 0


def test_agent_persists_failure_report_when_step_limit_is_reached(tmp_path: Path) -> None:
    provider = ScriptedProvider([ModelResponse()])
    run_store = RunStore(tmp_path / ".picoclaw" / "runs")
    agent = Agent(
        provider,
        build_coding_registry(tmp_workspace(tmp_path), approval_mode="never"),
        max_steps=1,
        run_store=run_store,
    )

    with pytest.raises(RuntimeError, match="max_steps=1"):
        agent.run("Never finishes")

    run_directory = next((tmp_path / ".picoclaw" / "runs").iterdir())
    report = json.loads((run_directory / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["error_type"] == "RuntimeError"


def tmp_workspace(path: Path) -> Workspace:
    return Workspace(path)
