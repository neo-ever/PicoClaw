"""Canonical messages shared by providers, the agent loop, and tools."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A provider-independent request to execute one tool."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The normalized result returned to the model after tool execution."""

    tool_call_id: str
    name: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Provider-independent token accounting for one or more model turns."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """One model turn: text, tool calls, or both."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Message:
    """A canonical conversation message kept by the runtime."""

    role: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


class ModelProvider(Protocol):
    """The only interface the Agent needs from a model backend."""

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[dict[str, Any]],
    ) -> ModelResponse:
        """Return the next assistant turn."""
