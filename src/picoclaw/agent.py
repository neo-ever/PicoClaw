"""The minimal bounded Agent Loop."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import Message, ModelProvider, TokenUsage, ToolResult
from .run_store import RunHandle, RunStore, utc_now
from .tools import ToolRegistry


@dataclass(frozen=True, slots=True)
class TraceEvent:
    kind: str
    detail: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class AgentResult:
    answer: str
    steps: int
    messages: tuple[Message, ...]
    trace: tuple[TraceEvent, ...]
    usage: TokenUsage
    run_id: str | None
    run_directory: str | None


class Agent:
    def __init__(
        self,
        provider: ModelProvider,
        tools: ToolRegistry,
        max_steps: int = 6,
        run_store: RunStore | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.provider = provider
        self.tools = tools
        self.max_steps = max_steps
        self.run_store = run_store

    def run(self, request: str) -> AgentResult:
        started_at = time.monotonic()
        messages = [Message(role="user", content=request)]
        trace: list[TraceEvent] = []
        total_usage = TokenUsage()
        tool_calls = 0
        tool_errors = 0
        handle = self.run_store.start() if self.run_store is not None else None

        def emit(kind: str, detail: str, metadata: dict[str, Any] | None = None) -> None:
            event = TraceEvent(kind, detail, metadata or {})
            trace.append(event)
            if self.run_store is not None and handle is not None:
                self.run_store.append_trace(handle, asdict(event))

        emit("run_started", request)

        try:
            for step in range(1, self.max_steps + 1):
                response = self.provider.complete(messages, self.tools.schemas())
                total_usage += response.usage
                emit(
                    "model_responded",
                    f"step={step}, total_tokens={response.usage.total_tokens}",
                    {
                        "step": step,
                        "finish_reason": response.finish_reason,
                        "usage": asdict(response.usage),
                    },
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
                        tool_calls += 1
                        result = self.tools.execute(call)
                        tool_errors += int(result.is_error)
                        messages.append(self._tool_message(result))
                        emit(
                            "tool_executed",
                            f"name={call.name}, error={str(result.is_error).lower()}",
                            {
                                "name": call.name,
                                "tool_call_id": call.id,
                                "is_error": result.is_error,
                                **result.metadata,
                            },
                        )
                    continue

                answer = response.content.strip()
                if answer:
                    emit("run_finished", f"step={step}", {"step": step, "status": "success"})
                    result = AgentResult(
                        answer,
                        step,
                        tuple(messages),
                        tuple(trace),
                        total_usage,
                        handle.run_id if handle else None,
                        str(handle.directory) if handle else None,
                    )
                    self._write_report(
                        handle,
                        status="success",
                        request=request,
                        answer=answer,
                        steps=step,
                        usage=total_usage,
                        tool_calls=tool_calls,
                        tool_errors=tool_errors,
                        duration_ms=int((time.monotonic() - started_at) * 1000),
                    )
                    return result

                emit("malformed_response", f"step={step}", {"step": step})

            raise RuntimeError(f"agent stopped after reaching max_steps={self.max_steps}")
        except Exception as exc:
            emit("run_failed", type(exc).__name__, {"status": "failed"})
            self._write_report(
                handle,
                status="failed",
                request=request,
                answer="",
                steps=len([event for event in trace if event.kind == "model_responded"]),
                usage=total_usage,
                tool_calls=tool_calls,
                tool_errors=tool_errors,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                error_type=type(exc).__name__,
            )
            raise

    @staticmethod
    def _tool_message(result: ToolResult) -> Message:
        return Message(
            role="tool",
            content=result.content,
            tool_call_id=result.tool_call_id,
            name=result.name,
        )

    def _write_report(
        self,
        handle: RunHandle | None,
        *,
        status: str,
        request: str,
        answer: str,
        steps: int,
        usage: TokenUsage,
        tool_calls: int,
        tool_errors: int,
        duration_ms: int,
        error_type: str | None = None,
    ) -> None:
        if self.run_store is None or handle is None:
            return
        self.run_store.write_report(
            handle,
            {
                "run_id": handle.run_id,
                "status": status,
                "request": request,
                "answer": answer,
                "steps": steps,
                "tool_calls": tool_calls,
                "tool_errors": tool_errors,
                "usage": asdict(usage),
                "duration_ms": duration_ms,
                "error_type": error_type,
                "finished_at": utc_now(),
            },
        )
