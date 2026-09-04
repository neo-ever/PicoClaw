from pathlib import Path

from picoclaw import (
    Agent,
    ContextBudget,
    ContextManager,
    MemoryManager,
    Message,
    ModelResponse,
    TokenCounter,
    ToolCall,
    ToolResult,
    Workspace,
)
from picoclaw.tools import build_read_only_registry


class RecordingProvider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(tuple(messages))
        return next(self.responses)


def test_context_compression_preserves_request_and_complete_tool_groups() -> None:
    counter = TokenCounter("unknown-local-model")
    manager = ContextManager(
        counter,
        ContextBudget(max_input_tokens=300, max_memory_tokens=60, max_summary_tokens=60),
    )
    messages = [Message(role="user", content="Fix the current project")]
    for index in range(8):
        call = ToolCall(f"call-{index}", "read_file", {"path": f"file-{index}.txt"})
        messages.extend(
            [
                Message(role="assistant", tool_calls=(call,)),
                Message(
                    role="tool",
                    name="read_file",
                    tool_call_id=call.id,
                    content=(f"result-{index} " * 80),
                ),
            ]
        )

    snapshot = manager.build(messages)

    assert snapshot.raw_tokens > snapshot.rendered_tokens
    assert snapshot.dropped_groups > 0
    assert snapshot.rendered_tokens <= 300
    assert snapshot.request_preserved is True
    assert snapshot.tool_groups_preserved is True
    assert any(message.content == "Fix the current project" for message in snapshot.messages)
    assert any(
        message.content.startswith("Compressed older history:") for message in snapshot.messages
    )
    call_ids = {
        call.id
        for message in snapshot.messages
        if message.role == "assistant"
        for call in message.tool_calls
    }
    result_ids = {message.tool_call_id for message in snapshot.messages if message.role == "tool"}
    assert call_ids == result_ids


def test_context_never_truncates_an_oversized_current_request() -> None:
    counter = TokenCounter("unknown-local-model")
    manager = ContextManager(
        counter,
        ContextBudget(max_input_tokens=100, max_memory_tokens=20, max_summary_tokens=20),
    )
    request = "important request " * 200

    snapshot = manager.build([Message(role="user", content=request)])

    assert snapshot.messages[-1].content == request
    assert snapshot.request_preserved is True
    assert snapshot.over_budget is True


def test_memory_persists_file_excerpt_as_episode(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "config.txt").write_text("default_budget=6", encoding="utf-8")
    memory = MemoryManager(workspace)
    memory.start_task()
    memory.observe_tool(
        ToolCall("read-1", "read_file", {"path": "config.txt"}),
        ToolResult("read-1", "read_file", "default_budget=6"),
    )

    memory.finish_task("Read the default budget", "The default budget is 6")
    reloaded = MemoryManager(workspace)
    entries = reloaded.relevant_entries("What is the default_budget in config.txt?")

    assert any("default_budget=6" in entry for entry in entries)
    assert any("[episodic" in entry for entry in entries)


def test_file_hash_change_invalidates_stale_memory(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    path = tmp_path / "config.txt"
    path.write_text("default_budget=6", encoding="utf-8")
    memory = MemoryManager(workspace)
    memory.observe_tool(
        ToolCall("read-1", "read_file", {"path": "config.txt"}),
        ToolResult("read-1", "read_file", "default_budget=6"),
    )
    memory.finish_task("Read config", "Budget is 6")

    path.write_text("default_budget=8", encoding="utf-8")
    entries = memory.relevant_entries("What is default_budget in config.txt?")

    assert all("default_budget=6" not in entry for entry in entries)
    assert any(record.source_path == "config.txt" for record in memory.last_invalidated)
    assert memory.episodic == []


def test_successful_write_replaces_old_memory_with_new_file_hash(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "config.txt").write_text("old", encoding="utf-8")
    memory = MemoryManager(workspace)
    memory.observe_tool(
        ToolCall("read-1", "read_file", {"path": "config.txt"}),
        ToolResult("read-1", "read_file", "old"),
    )
    assert len(memory.working) == 1

    (tmp_path / "config.txt").write_text("new", encoding="utf-8")
    memory.observe_tool(
        ToolCall("write-1", "write_file", {"path": "config.txt", "content": "new"}),
        ToolResult("write-1", "write_file", "wrote 3 bytes"),
    )

    assert len(memory.working) == 1
    assert "Current content written to config.txt:\nnew" in memory.working[0].content
    assert memory.working[0].source_sha256 == workspace.sha256("config.txt")
    assert len(memory.last_invalidated) == 1


def test_durable_memory_is_persisted_and_retrieved(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    memory = MemoryManager(workspace)

    memory.add_durable("Project commands should use uv run pytest")
    reloaded = MemoryManager(workspace)

    assert any("uv run pytest" in entry for entry in reloaded.relevant_entries("test commands"))


def test_agent_injects_prior_file_memory_without_another_tool_call(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path)
    (tmp_path / "config.txt").write_text("project_name=PicoClaw", encoding="utf-8")
    memory = MemoryManager(workspace)
    context = ContextManager(TokenCounter("unknown-local-model"))

    first_provider = RecordingProvider(
        [
            ModelResponse(tool_calls=(ToolCall("read-1", "read_file", {"path": "config.txt"}),)),
            ModelResponse(content="The project name is PicoClaw."),
        ]
    )
    Agent(
        first_provider,
        build_read_only_registry(workspace),
        context_manager=context,
        memory_manager=memory,
    ).run("Read config.txt and find project_name")

    second_provider = RecordingProvider([ModelResponse(content="It is PicoClaw.")])
    second_result = Agent(
        second_provider,
        build_read_only_registry(workspace),
        context_manager=context,
        memory_manager=memory,
    ).run("What was the project_name in config.txt?")

    assert second_result.answer == "It is PicoClaw."
    assert len(second_provider.requests) == 1
    assert second_provider.requests[0][0].role == "system"
    assert "project_name=PicoClaw" in second_provider.requests[0][0].content
