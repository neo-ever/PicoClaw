from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest
from openai import APIConnectionError

from picoclaw import (
    Agent,
    Message,
    OllamaConfig,
    OllamaTextProvider,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderConnectionError,
    ProviderProtocolError,
    ToolCall,
    Workspace,
)
from picoclaw.tools import build_read_only_registry


class FakeCompletions:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response=None, error=None):
        self.completions = FakeCompletions(response=response, error=error)
        self.chat = SimpleNamespace(completions=self.completions)


def make_completion(arguments='{"path":"README.md"}'):
    function = SimpleNamespace(name="read_file", arguments=arguments)
    tool_call = SimpleNamespace(id="call-1", function=function)
    message = SimpleNamespace(content="I will inspect the file.", tool_calls=[tool_call])
    choice = SimpleNamespace(message=message, finish_reason="tool_calls")
    usage = SimpleNamespace(prompt_tokens=20, completion_tokens=5, total_tokens=25)
    return SimpleNamespace(choices=[choice], usage=usage)


def make_final_completion(content="The project is PicoClaw."):
    message = SimpleNamespace(content=content, tool_calls=[])
    choice = SimpleNamespace(message=message, finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=40, completion_tokens=6, total_tokens=46)
    return SimpleNamespace(choices=[choice], usage=usage)


def test_provider_config_uses_safe_defaults_and_hides_key() -> None:
    config = ProviderConfig(model="demo-model", api_key="secret-key")

    assert config.base_url is None
    assert config.timeout == 60.0
    assert "secret-key" not in repr(config)


def test_provider_config_is_immutable() -> None:
    config = ProviderConfig(model="demo-model", api_key="demo-key")

    with pytest.raises(FrozenInstanceError):
        config.model = "changed-model"  # type: ignore[misc]


def test_provider_config_loads_from_environment_mapping() -> None:
    config = ProviderConfig.from_env(
        {
            "PICOCLAW_MODEL": "compatible-model",
            "PICOCLAW_API_KEY": "secret-key",
            "PICOCLAW_BASE_URL": "https://provider.example/v1",
            "PICOCLAW_TIMEOUT": "12.5",
        }
    )

    assert config.model == "compatible-model"
    assert config.api_key == "secret-key"
    assert config.base_url == "https://provider.example/v1"
    assert config.timeout == 12.5


def test_provider_config_reports_missing_environment_variables() -> None:
    with pytest.raises(ValueError, match="PICOCLAW_MODEL, PICOCLAW_API_KEY"):
        ProviderConfig.from_env({})


def test_provider_parses_native_tool_call_and_usage() -> None:
    client = FakeClient(response=make_completion())
    provider = OpenAICompatibleProvider(
        ProviderConfig(model="demo-model", api_key="demo-key"),
        client=client,
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object"},
            },
        }
    ]

    response = provider.complete([Message(role="user", content="Read README")], tools)

    assert response.tool_calls == (ToolCall("call-1", "read_file", {"path": "README.md"}),)
    assert response.usage.total_tokens == 25
    assert response.finish_reason == "tool_calls"
    request = client.completions.requests[0]
    assert request["model"] == "demo-model"
    assert request["tool_choice"] == "auto"
    assert request["messages"] == [{"role": "user", "content": "Read README"}]


def test_provider_preserves_assistant_tool_call_and_tool_result_pair() -> None:
    call = ToolCall("call-1", "read_file", {"path": "README.md"})
    messages = [
        Message(role="assistant", tool_calls=(call,)),
        Message(role="tool", tool_call_id="call-1", name="read_file", content="# PicoClaw"),
    ]

    serialized = OpenAICompatibleProvider.to_api_messages(messages)

    assert serialized[0]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
    assert serialized[1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "# PicoClaw",
    }


def test_provider_rejects_malformed_tool_arguments() -> None:
    with pytest.raises(ProviderProtocolError, match="malformed JSON"):
        OpenAICompatibleProvider.from_api_completion(make_completion(arguments="{bad-json"))


def test_provider_normalizes_connection_errors() -> None:
    client = FakeClient(error=APIConnectionError(request=object()))
    provider = OpenAICompatibleProvider(
        ProviderConfig(model="demo-model", api_key="demo-key"),
        client=client,
    )

    with pytest.raises(ProviderConnectionError, match="connection failed"):
        provider.complete([Message(role="user", content="hello")], [])


def test_openai_provider_runs_full_agent_tool_round_trip(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# PicoClaw", encoding="utf-8")

    class SequentialCompletions:
        def __init__(self):
            self.responses = iter([make_completion(), make_final_completion()])
            self.requests = []

        def create(self, **request):
            self.requests.append(request)
            return next(self.responses)

    completions = SequentialCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = OpenAICompatibleProvider(
        ProviderConfig(model="demo-model", api_key="demo-key"),
        client=client,
    )
    agent = Agent(provider, build_read_only_registry(Workspace(tmp_path)))

    result = agent.run("Read README and tell me the project name")

    assert result.answer == "The project is PicoClaw."
    assert result.usage.total_tokens == 71
    second_messages = completions.requests[1]["messages"]
    assert [message["role"] for message in second_messages] == ["user", "assistant", "tool"]
    assert second_messages[-1]["tool_call_id"] == "call-1"
    assert second_messages[-1]["content"] == "# PicoClaw"


def test_ollama_text_provider_parses_tool_call_and_usage() -> None:
    captured = {}

    def transport(url, payload, timeout):
        captured.update(url=url, payload=payload, timeout=timeout)
        return {
            "response": '<tool>{"name":"read_file","arguments":{"path":"README.md"}}</tool>',
            "prompt_eval_count": 30,
            "eval_count": 8,
        }

    provider = OllamaTextProvider(OllamaConfig(model="local-model"), transport=transport)

    response = provider.complete([Message(role="user", content="Read README")], [])

    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    assert response.usage.total_tokens == 38
    assert captured["url"] == "http://127.0.0.1:11434/api/generate"
    assert captured["payload"]["model"] == "local-model"


def test_ollama_text_provider_parses_final_answer() -> None:
    provider = OllamaTextProvider(OllamaConfig(model="local-model"))

    response = provider.parse_text_response("<final>The project is PicoClaw.</final>")

    assert response.content == "The project is PicoClaw."
    assert response.finish_reason == "stop"


def test_ollama_text_provider_rejects_unstructured_output() -> None:
    provider = OllamaTextProvider(OllamaConfig(model="local-model"))

    with pytest.raises(ProviderProtocolError, match="must return either"):
        provider.parse_text_response("I think the answer is PicoClaw")
