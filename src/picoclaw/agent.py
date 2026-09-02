"""The minimal bounded Agent Loop."""

from __future__ import annotations

from dataclasses import dataclass

from .models import Message, ModelProvider, TokenUsage, ToolResult
from .tools import ToolRegistry


@dataclass(frozen=True, slots=True)
class TraceEvent:
    kind: str
    detail: str


@dataclass(frozen=True, slots=True)
class AgentResult:
    answer: str
    steps: int
    messages: tuple[Message, ...]
    trace: tuple[TraceEvent, ...]
    usage: TokenUsage


class Agent:
    def __init__(self, provider: ModelProvider, tools: ToolRegistry, max_steps: int = 6) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.provider = provider
        self.tools = tools
        self.max_steps = max_steps

    def run(self, request: str) -> AgentResult:
        messages = [Message(role="user", content=request)]
        trace = [TraceEvent("run_started", request)]
        total_usage = TokenUsage()

        for step in range(1, self.max_steps + 1):
            response = self.provider.complete(messages, self.tools.schemas())
            total_usage += response.usage
            trace.append(
                TraceEvent(
                    "model_responded",
                    f"step={step}, total_tokens={response.usage.total_tokens}",
                )
            )
            messages.append(
                Message(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )

            if response.tool_calls:
                for call in response.tool_calls:
                    result = self.tools.execute(call)
                    messages.append(self._tool_message(result))
                    trace.append(
                        TraceEvent(
                            "tool_executed",
                            f"name={call.name}, error={str(result.is_error).lower()}",
                        )
                    )
                continue

            answer = response.content.strip()
            if answer:
                trace.append(TraceEvent("run_finished", f"step={step}"))
                return AgentResult(answer, step, tuple(messages), tuple(trace), total_usage)

            trace.append(TraceEvent("malformed_response", f"step={step}"))

        raise RuntimeError(f"agent stopped after reaching max_steps={self.max_steps}")

    @staticmethod
    def _tool_message(result: ToolResult) -> Message:
        return Message(
            role="tool",
            content=result.content,
            tool_call_id=result.tool_call_id,
            name=result.name,
        )
