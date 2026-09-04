"""The minimal bounded Agent Loop."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .context_manager import ContextManager, ContextSnapshot
from .memory import MemoryManager
from .models import Message, ModelProvider, TokenUsage, ToolResult
from .run_store import RunHandle, RunStore, utc_now
from .skills import SkillCatalog, SkillSelection
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


@dataclass(frozen=True, slots=True)
class AgentRunBudget:
    """Hard limits applied to one Agent invocation by the Goal Loop."""

    max_model_steps: int
    max_tool_calls: int
    max_total_tokens: int
    max_seconds: float

    def __post_init__(self) -> None:
        if self.max_model_steps < 1:
            raise ValueError("max_model_steps must be positive")
        if min(self.max_tool_calls, self.max_total_tokens) < 0:
            raise ValueError("tool and token budgets cannot be negative")
        if self.max_seconds <= 0:
            raise ValueError("max_seconds must be positive")


class AgentBudgetExceeded(RuntimeError):
    def __init__(
        self,
        reason: str,
        *,
        model_steps: int,
        tool_calls: int,
        usage: TokenUsage,
    ) -> None:
        super().__init__(f"agent run budget exceeded: {reason}")
        self.reason = reason
        self.model_steps = model_steps
        self.tool_calls = tool_calls
        self.usage = usage


class Agent:
    def __init__(
        self,
        provider: ModelProvider,
        tools: ToolRegistry,
        max_steps: int = 6,
        run_store: RunStore | None = None,
        context_manager: ContextManager | None = None,
        memory_manager: MemoryManager | None = None,
        skill_catalog: SkillCatalog | None = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.provider = provider
        self.tools = tools
        self.max_steps = max_steps
        self.run_store = run_store
        self.context_manager = context_manager
        self.memory_manager = memory_manager
        self.skill_catalog = skill_catalog

    def run(self, request: str, budget: AgentRunBudget | None = None) -> AgentResult:
        started_at = time.monotonic()
        messages = [Message(role="user", content=request)]
        trace: list[TraceEvent] = []
        total_usage = TokenUsage()
        tool_calls = 0
        tool_errors = 0
        model_steps = 0
        context_builds: list[ContextSnapshot] = []
        skill_selection: SkillSelection | None = None
        handle = self.run_store.start() if self.run_store is not None else None

        def emit(kind: str, detail: str, metadata: dict[str, Any] | None = None) -> None:
            event = TraceEvent(kind, detail, metadata or {})
            trace.append(event)
            if self.run_store is not None and handle is not None:
                self.run_store.append_trace(handle, asdict(event))

        emit("run_started", request)
        if self.memory_manager is not None:
            self.memory_manager.start_task()

        try:
            if self.skill_catalog is not None:
                skill_selection = self.skill_catalog.select(request)
                emit(
                    "skills_selected",
                    ", ".join(skill_selection.names) or "none",
                    {
                        "catalog_size": skill_selection.catalog_size,
                        "selected": list(skill_selection.names),
                        "metadata_chars": skill_selection.metadata_chars,
                        "loaded_chars": skill_selection.loaded_chars,
                    },
                )
            step_limit = (
                min(self.max_steps, budget.max_model_steps)
                if budget is not None
                else self.max_steps
            )
            for step in range(1, step_limit + 1):
                self._check_run_budget(
                    budget,
                    started_at,
                    model_steps=model_steps,
                    tool_calls=tool_calls,
                    usage=total_usage,
                )
                model_messages: list[Message] | tuple[Message, ...] = messages
                if self.context_manager is not None:
                    memory_entries = (
                        self.memory_manager.relevant_entries(request)
                        if self.memory_manager is not None
                        else []
                    )
                    skill_instructions = (
                        skill_selection.prompt_blocks() if skill_selection is not None else ()
                    )
                    snapshot = self.context_manager.build(
                        messages,
                        memory_entries,
                        skill_instructions,
                    )
                    context_builds.append(snapshot)
                    model_messages = snapshot.messages
                    emit(
                        "context_built",
                        (
                            f"step={step}, raw_tokens={snapshot.raw_tokens}, "
                            f"rendered_tokens={snapshot.rendered_tokens}"
                        ),
                        {
                            "step": step,
                            "raw_tokens": snapshot.raw_tokens,
                            "rendered_tokens": snapshot.rendered_tokens,
                            "dropped_groups": snapshot.dropped_groups,
                            "memory_items": snapshot.memory_items,
                            "skill_items": snapshot.skill_items,
                            "request_preserved": snapshot.request_preserved,
                            "tool_groups_preserved": snapshot.tool_groups_preserved,
                            "over_budget": snapshot.over_budget,
                            "token_counter": snapshot.token_counter,
                            "invalidated_memories": (
                                len(self.memory_manager.last_invalidated)
                                if self.memory_manager is not None
                                else 0
                            ),
                        },
                    )
                response = self.provider.complete(model_messages, self.tools.schemas())
                model_steps = step
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
                if budget is not None and total_usage.total_tokens > budget.max_total_tokens:
                    raise AgentBudgetExceeded(
                        "total_tokens",
                        model_steps=model_steps,
                        tool_calls=tool_calls,
                        usage=total_usage,
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
                        self._check_run_budget(
                            budget,
                            started_at,
                            model_steps=model_steps,
                            tool_calls=tool_calls,
                            usage=total_usage,
                        )
                        if budget is not None and tool_calls >= budget.max_tool_calls:
                            raise AgentBudgetExceeded(
                                "tool_calls",
                                model_steps=model_steps,
                                tool_calls=tool_calls,
                                usage=total_usage,
                            )
                        tool_calls += 1
                        result = self.tools.execute(call)
                        if self.memory_manager is not None:
                            self.memory_manager.observe_tool(call, result)
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
                    if self.memory_manager is not None:
                        self.memory_manager.finish_task(request, answer)
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
                        context_builds=context_builds,
                        skill_selection=skill_selection,
                    )
                    return result

                emit("malformed_response", f"step={step}", {"step": step})

            if budget is not None:
                raise AgentBudgetExceeded(
                    "model_steps",
                    model_steps=model_steps,
                    tool_calls=tool_calls,
                    usage=total_usage,
                )
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
                context_builds=context_builds,
                skill_selection=skill_selection,
            )
            raise

    @staticmethod
    def _check_run_budget(
        budget: AgentRunBudget | None,
        started_at: float,
        *,
        model_steps: int,
        tool_calls: int,
        usage: TokenUsage,
    ) -> None:
        if budget is None:
            return
        if time.monotonic() - started_at >= budget.max_seconds:
            raise AgentBudgetExceeded(
                "time",
                model_steps=model_steps,
                tool_calls=tool_calls,
                usage=usage,
            )

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
        context_builds: list[ContextSnapshot] | None = None,
        skill_selection: SkillSelection | None = None,
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
                "context": self._context_report(context_builds or []),
                "skills": {
                    "catalog_size": skill_selection.catalog_size if skill_selection else 0,
                    "selected": list(skill_selection.names) if skill_selection else [],
                    "metadata_chars": skill_selection.metadata_chars if skill_selection else 0,
                    "loaded_chars": skill_selection.loaded_chars if skill_selection else 0,
                },
                "invalidated_memories": (
                    len(self.memory_manager.last_invalidated)
                    if self.memory_manager is not None
                    else 0
                ),
                "finished_at": utc_now(),
            },
        )

    @staticmethod
    def _context_report(snapshots: list[ContextSnapshot]) -> dict[str, Any]:
        if not snapshots:
            return {"builds": 0}
        return {
            "builds": len(snapshots),
            "raw_tokens_total": sum(item.raw_tokens for item in snapshots),
            "rendered_tokens_total": sum(item.rendered_tokens for item in snapshots),
            "dropped_groups_total": sum(item.dropped_groups for item in snapshots),
            "request_preserved": all(item.request_preserved for item in snapshots),
            "tool_groups_preserved": all(item.tool_groups_preserved for item in snapshots),
            "skill_items_max": max(item.skill_items for item in snapshots),
            "token_counter": snapshots[-1].token_counter,
        }
