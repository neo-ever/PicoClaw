from pathlib import Path

import pytest

from picoclaw import Agent, ModelResponse, TokenUsage, ToolCall, Workspace
from picoclaw.tools import build_read_only_registry


class ScriptedProvider:
    """A deterministic model that lets us test the runtime without an API key."""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append((tuple(messages), tuple(tools)))
        return next(self.responses)


def test_agent_reads_a_file_then_answers(tmp_path: Path) -> None:
    (tmp_path / "project.txt").write_text("project_name=PicoClaw", encoding="utf-8")
    provider = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=(ToolCall("call-1", "read_file", {"path": "project.txt"}),),
                usage=TokenUsage(prompt_tokens=10, completion_tokens=3, total_tokens=13),
            ),
            ModelResponse(
                content="The project is PicoClaw.",
                usage=TokenUsage(prompt_tokens=15, completion_tokens=5, total_tokens=20),
            ),
        ]
    )
    agent = Agent(provider, build_read_only_registry(Workspace(tmp_path)))

    result = agent.run("What is the project name?")

    assert result.answer == "The project is PicoClaw."
    assert result.steps == 2
    assert result.messages[-2].role == "tool"
    assert result.messages[-2].content == "project_name=PicoClaw"
    assert result.usage.total_tokens == 33
    assert [event.kind for event in result.trace].count("tool_executed") == 1


def test_workspace_rejects_path_escape(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)

    with pytest.raises(ValueError, match="path escapes workspace"):
        workspace.resolve("../secret.txt")


def test_unknown_tool_returns_error_to_model(tmp_path: Path) -> None:
    registry = build_read_only_registry(Workspace(tmp_path))

    result = registry.execute(ToolCall("call-2", "delete_everything", {}))

    assert result.is_error is True
    assert result.content == "unknown tool: delete_everything"
