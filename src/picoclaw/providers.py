"""Model provider configuration, protocol translation, and error normalization."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    OpenAI,
    OpenAIError,
    PermissionDeniedError,
    RateLimitError,
)

from .models import Message, ModelResponse, TokenUsage, ToolCall


class ProviderError(RuntimeError):
    """Base error exposed by provider adapters."""


class ProviderAuthenticationError(ProviderError):
    """Credentials are missing, invalid, or not authorized."""


class ProviderConnectionError(ProviderError):
    """The provider could not be reached before the timeout."""


class ProviderRateLimitError(ProviderError):
    """The provider rejected the request because of a rate or quota limit."""


class ProviderRequestError(ProviderError):
    """The provider rejected an invalid request."""


class ProviderProtocolError(ProviderError):
    """The provider response cannot be converted to PicoClaw's canonical protocol."""


class OllamaTransport(Protocol):
    """Injectable HTTP boundary used to test Ollama without a running server."""

    def __call__(self, url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Connection settings shared by model provider adapters."""

    model: str
    api_key: str = field(repr=False)
    base_url: str | None = None
    timeout: float = 60.0

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if not self.api_key.strip():
            raise ValueError("api_key must not be empty")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ProviderConfig:
        """Load configuration without placing credentials in source code."""

        source = os.environ if environ is None else environ
        model = source.get("PICOCLAW_MODEL", "").strip()
        api_key = source.get("PICOCLAW_API_KEY", "").strip()
        missing = [
            name
            for name, value in (
                ("PICOCLAW_MODEL", model),
                ("PICOCLAW_API_KEY", api_key),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"missing required environment variables: {', '.join(missing)}")

        raw_timeout = source.get("PICOCLAW_TIMEOUT", "60").strip()
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise ValueError("PICOCLAW_TIMEOUT must be a number") from exc

        base_url = source.get("PICOCLAW_BASE_URL", "").strip() or None
        return cls(model=model, api_key=api_key, base_url=base_url, timeout=timeout)


@dataclass(frozen=True, slots=True)
class OllamaConfig:
    """Settings for a local Ollama text-protocol fallback."""

    model: str
    host: str = "http://127.0.0.1:11434"
    timeout: float = 300.0
    temperature: float = 0.2

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if not self.host.strip():
            raise ValueError("host must not be empty")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> OllamaConfig:
        source = os.environ if environ is None else environ
        model = (
            source.get("PICOCLAW_OLLAMA_MODEL", "").strip()
            or source.get("PICOCLAW_MODEL", "").strip()
        )
        if not model:
            raise ValueError(
                "missing required environment variable: PICOCLAW_OLLAMA_MODEL or PICOCLAW_MODEL"
            )
        try:
            timeout = float(source.get("PICOCLAW_OLLAMA_TIMEOUT", "300").strip())
            temperature = float(source.get("PICOCLAW_OLLAMA_TEMPERATURE", "0.2").strip())
        except ValueError as exc:
            raise ValueError("Ollama timeout and temperature must be numbers") from exc
        host = source.get("PICOCLAW_OLLAMA_HOST", "http://127.0.0.1:11434").strip()
        return cls(model=model, host=host, timeout=timeout, temperature=temperature)


class OpenAICompatibleProvider:
    """Translate canonical PicoClaw messages to OpenAI-compatible Chat Completions."""

    def __init__(self, config: ProviderConfig, client: Any | None = None) -> None:
        self.config = config
        if client is not None:
            self._client = client
            return

        client_options: dict[str, Any] = {
            "api_key": config.api_key,
            "timeout": config.timeout,
        }
        if config.base_url is not None:
            client_options["base_url"] = config.base_url
        self._client = OpenAI(**client_options)

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> ModelResponse:
        request: dict[str, Any] = {
            "model": self.config.model,
            "messages": self.to_api_messages(messages),
        }
        if tools:
            request["tools"] = list(tools)
            request["tool_choice"] = "auto"

        try:
            completion = self._client.chat.completions.create(**request)
        except (AuthenticationError, PermissionDeniedError) as exc:
            raise ProviderAuthenticationError("provider authentication failed") from exc
        except RateLimitError as exc:
            raise ProviderRateLimitError("provider rate or quota limit reached") from exc
        except (APIConnectionError, APITimeoutError) as exc:
            raise ProviderConnectionError("provider connection failed or timed out") from exc
        except BadRequestError as exc:
            raise ProviderRequestError("provider rejected the request") from exc
        except OpenAIError as exc:
            raise ProviderError("provider request failed") from exc

        return self.from_api_completion(completion)

    @staticmethod
    def to_api_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
        """Serialize canonical history while preserving tool-call/result pairs."""

        serialized: list[dict[str, Any]] = []
        for message in messages:
            if message.role in {"system", "user"}:
                serialized.append({"role": message.role, "content": message.content})
                continue

            if message.role == "assistant":
                item: dict[str, Any] = {
                    "role": "assistant",
                    "content": message.content or None,
                }
                if message.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in message.tool_calls
                    ]
                serialized.append(item)
                continue

            if message.role == "tool":
                if not message.tool_call_id:
                    raise ProviderProtocolError("tool message is missing tool_call_id")
                serialized.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id,
                        "content": message.content,
                    }
                )
                continue

            raise ProviderProtocolError(f"unsupported message role: {message.role}")

        return serialized

    @staticmethod
    def from_api_completion(completion: Any) -> ModelResponse:
        """Normalize one SDK completion into PicoClaw's provider-independent response."""

        choices = getattr(completion, "choices", None)
        if not choices:
            raise ProviderProtocolError("provider response contains no choices")

        choice = choices[0]
        message = getattr(choice, "message", None)
        if message is None:
            raise ProviderProtocolError("provider response contains no assistant message")

        tool_calls = tuple(
            OpenAICompatibleProvider._parse_tool_call(item)
            for item in (getattr(message, "tool_calls", None) or [])
        )
        content = getattr(message, "content", "") or ""
        if not isinstance(content, str):
            content = str(content)

        raw_usage = getattr(completion, "usage", None)
        usage = TokenUsage(
            prompt_tokens=int(getattr(raw_usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(raw_usage, "completion_tokens", 0) or 0),
            total_tokens=int(getattr(raw_usage, "total_tokens", 0) or 0),
        )
        finish_reason = getattr(choice, "finish_reason", None)
        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=str(finish_reason) if finish_reason is not None else None,
        )

    @staticmethod
    def _parse_tool_call(item: Any) -> ToolCall:
        call_id = str(getattr(item, "id", "") or "").strip()
        function = getattr(item, "function", None)
        name = str(getattr(function, "name", "") or "").strip()
        raw_arguments = getattr(function, "arguments", "{}")

        if not call_id or not name:
            raise ProviderProtocolError("tool call is missing id or function name")
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        else:
            try:
                arguments = json.loads(str(raw_arguments or "{}"))
            except json.JSONDecodeError as exc:
                raise ProviderProtocolError(
                    f"tool '{name}' returned malformed JSON arguments"
                ) from exc
        if not isinstance(arguments, dict):
            raise ProviderProtocolError(f"tool '{name}' arguments must be a JSON object")

        return ToolCall(id=call_id, name=name, arguments=arguments)


class OllamaTextProvider:
    """Fallback for local models that cannot return native structured tool calls."""

    TOOL_PATTERN = re.compile(r"<tool>\s*(\{.*?\})\s*</tool>", re.DOTALL)
    FINAL_PATTERN = re.compile(r"<final>\s*(.*?)\s*</final>", re.DOTALL)

    def __init__(self, config: OllamaConfig, transport: OllamaTransport | None = None) -> None:
        self.config = config
        self._transport = transport or self._post_json
        self._tool_call_sequence = 0

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> ModelResponse:
        payload = {
            "model": self.config.model,
            "prompt": self.render_prompt(messages, tools),
            "stream": False,
            "options": {"temperature": self.config.temperature},
        }
        endpoint = self.config.host.rstrip("/") + "/api/generate"
        try:
            data = self._transport(endpoint, payload, self.config.timeout)
        except ProviderError:
            raise
        except (TimeoutError, urllib.error.URLError) as exc:
            raise ProviderConnectionError("Ollama connection failed or timed out") from exc

        raw = str(data.get("response", "") or "").strip()
        usage = TokenUsage(
            prompt_tokens=int(data.get("prompt_eval_count", 0) or 0),
            completion_tokens=int(data.get("eval_count", 0) or 0),
            total_tokens=int(data.get("prompt_eval_count", 0) or 0)
            + int(data.get("eval_count", 0) or 0),
        )
        return self.parse_text_response(raw, usage)

    def parse_text_response(self, raw: str, usage: TokenUsage | None = None) -> ModelResponse:
        tool_match = self.TOOL_PATTERN.search(raw)
        final_match = self.FINAL_PATTERN.search(raw)
        usage = usage or TokenUsage()

        if tool_match and (not final_match or tool_match.start() < final_match.start()):
            try:
                payload = json.loads(tool_match.group(1))
            except json.JSONDecodeError as exc:
                raise ProviderProtocolError("Ollama returned malformed tool JSON") from exc
            if not isinstance(payload, dict):
                raise ProviderProtocolError("Ollama tool payload must be a JSON object")
            name = str(payload.get("name", "") or "").strip()
            arguments = payload.get("arguments", payload.get("args", {}))
            if not name or not isinstance(arguments, dict):
                raise ProviderProtocolError("Ollama tool call requires a name and object arguments")
            self._tool_call_sequence += 1
            call = ToolCall(
                id=f"ollama-call-{self._tool_call_sequence}",
                name=name,
                arguments=arguments,
            )
            return ModelResponse(tool_calls=(call,), usage=usage, finish_reason="tool_call")

        if final_match:
            answer = final_match.group(1).strip()
            if not answer:
                raise ProviderProtocolError("Ollama returned an empty final answer")
            return ModelResponse(content=answer, usage=usage, finish_reason="stop")

        raise ProviderProtocolError(
            "Ollama must return either <tool>{...}</tool> or <final>...</final>"
        )

    @staticmethod
    def render_prompt(
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> str:
        history: list[str] = []
        for message in messages:
            if message.role == "assistant" and message.tool_calls:
                calls = [
                    {"id": call.id, "name": call.name, "arguments": call.arguments}
                    for call in message.tool_calls
                ]
                history.append(f"assistant tool_calls: {json.dumps(calls, ensure_ascii=False)}")
            elif message.role == "tool":
                history.append(
                    f"tool[{message.name or 'unknown'}#{message.tool_call_id or 'unknown'}]: "
                    f"{message.content}"
                )
            else:
                history.append(f"{message.role}: {message.content}")

        return (
            "You are a coding agent. Choose exactly one next action.\n"
            "Available tools (JSON Schema):\n"
            f"{json.dumps(list(tools), ensure_ascii=False)}\n\n"
            "Conversation:\n"
            f"{'\n'.join(history)}\n\n"
            "If a tool is needed, return exactly:\n"
            '<tool>{"name":"tool_name","arguments":{}}</tool>\n'
            "If the task is complete, return exactly:\n"
            "<final>your answer</final>"
        )

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise ProviderAuthenticationError("Ollama request was not authorized") from exc
            if exc.code == 429:
                raise ProviderRateLimitError("Ollama rate limit reached") from exc
            if 400 <= exc.code < 500:
                raise ProviderRequestError(
                    f"Ollama rejected the request with HTTP {exc.code}"
                ) from exc
            raise ProviderConnectionError(f"Ollama failed with HTTP {exc.code}") from exc
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderProtocolError("Ollama returned non-JSON HTTP content") from exc
        if not isinstance(data, dict):
            raise ProviderProtocolError("Ollama HTTP response must be a JSON object")
        return data
