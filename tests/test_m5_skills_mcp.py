from pathlib import Path
from types import SimpleNamespace

import pytest

from picoclaw import (
    Agent,
    ApprovalMode,
    Approver,
    ContextManager,
    MCPAdapterError,
    MCPServerConfig,
    MCPToolAdapter,
    ModelResponse,
    SkillCatalog,
    TokenCounter,
    ToolCall,
    ToolRegistry,
)


def write_skill(root: Path, name: str, description: str, body: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_skill_scan_is_progressive_and_loads_only_a_match(tmp_path: Path) -> None:
    write_skill(tmp_path, "python-test", "Run Python tests with pytest", "Use uv run pytest.")
    write_skill(tmp_path, "docs", "Write product documentation", "D" * 1_000)
    catalog = SkillCatalog([tmp_path], max_body_bytes=200)

    metadata = catalog.scan()
    selection = catalog.select("Please run the Python pytest suite")

    assert len(metadata) == 2
    assert selection.names == ("python-test",)
    assert selection.loaded_chars == len("Use uv run pytest.")


def test_agent_injects_selected_skill_as_guarded_system_guidance(tmp_path: Path) -> None:
    write_skill(tmp_path, "repository-summary", "Summarize a code repository", "Read README first.")

    class Provider:
        def __init__(self) -> None:
            self.messages = ()

        def complete(self, messages, tools):
            del tools
            self.messages = tuple(messages)
            return ModelResponse(content="done")

    provider = Provider()
    result = Agent(
        provider,
        ToolRegistry(),
        context_manager=ContextManager(TokenCounter("unknown-local-model")),
        skill_catalog=SkillCatalog([tmp_path]),
    ).run("Summarize this repository")

    assert result.answer == "done"
    assert provider.messages[0].role == "system"
    assert "untrusted guidance" in provider.messages[0].content
    assert "Read README first" in provider.messages[0].content
    event = next(item for item in result.trace if item.kind == "skills_selected")
    assert event.metadata["selected"] == ["repository-summary"]


def test_skill_selection_supports_chinese_bigrams(tmp_path: Path) -> None:
    write_skill(tmp_path, "repo-summary", "用关键文件总结本地代码仓库", "先读取 README。")

    selection = SkillCatalog([tmp_path]).select("请帮我总结这个代码仓库")

    assert selection.names == ("repo-summary",)


class FakeMCPBackend:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0
        self.calls = []

    def factory(self, params, timeout):
        backend = self

        class Client:
            async def __aenter__(self):
                backend.entered += 1
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                backend.exited += 1

            async def list_tools(self, cursor=None):
                del cursor
                tool = SimpleNamespace(
                    name="word_count",
                    description="Count words",
                    input_schema={
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                )
                return SimpleNamespace(tools=[tool], next_cursor=None)

            async def call_tool(self, name, arguments):
                backend.calls.append((name, arguments))
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="2")],
                    structured_content=None,
                    is_error=False,
                )

        del params, timeout
        return Client()


def mcp_adapter(tmp_path: Path, backend: FakeMCPBackend) -> MCPToolAdapter:
    return MCPToolAdapter(
        MCPServerConfig(
            name="demo",
            command="python",
            cwd=tmp_path,
            allowed_tools=frozenset({"word_count"}),
        ),
        client_factory=backend.factory,
    )


def test_mcp_tool_is_namespaced_and_still_requires_runtime_approval(tmp_path: Path) -> None:
    backend = FakeMCPBackend()
    registry = ToolRegistry(Approver(ApprovalMode.NEVER))
    descriptor = mcp_adapter(tmp_path, backend).register_tools(registry)[0]

    result = registry.execute(ToolCall("call-1", descriptor.exposed_name, {"text": "hello world"}))

    assert descriptor.exposed_name == "mcp__demo__word_count"
    assert result.is_error is True
    assert result.metadata["reason"] == "approval_denied"
    assert backend.calls == []
    assert backend.entered == backend.exited == 1


def test_approved_mcp_tool_opens_and_closes_a_fresh_client(tmp_path: Path) -> None:
    backend = FakeMCPBackend()
    registry = ToolRegistry(Approver(ApprovalMode.AUTO))
    descriptor = mcp_adapter(tmp_path, backend).register_tools(registry)[0]

    result = registry.execute(ToolCall("call-1", descriptor.exposed_name, {"text": "hello world"}))

    assert result.content == "2"
    assert result.is_error is False
    assert backend.calls == [("word_count", {"text": "hello world"})]
    assert backend.entered == backend.exited == 2


def test_mcp_arguments_are_validated_before_remote_call(tmp_path: Path) -> None:
    backend = FakeMCPBackend()
    registry = ToolRegistry(Approver(ApprovalMode.AUTO))
    descriptor = mcp_adapter(tmp_path, backend).register_tools(registry)[0]

    result = registry.execute(ToolCall("call-1", descriptor.exposed_name, {"text": 123}))

    assert result.is_error is True
    assert "must have JSON type string" in result.content
    assert backend.calls == []


def test_mcp_discovery_rejects_missing_allowlisted_tool(tmp_path: Path) -> None:
    backend = FakeMCPBackend()
    adapter = MCPToolAdapter(
        MCPServerConfig(
            name="demo",
            command="python",
            cwd=tmp_path,
            allowed_tools=frozenset({"not_advertised"}),
        ),
        client_factory=backend.factory,
    )

    with pytest.raises(MCPAdapterError, match="not advertised"):
        adapter.discover()
