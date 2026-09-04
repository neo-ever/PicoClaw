"""Offline demonstration of memory reuse and SHA-256 invalidation."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from .agent import Agent, AgentResult
from .context_manager import ContextBudget, ContextManager, TokenCounter
from .memory import MemoryManager
from .models import Message, ModelResponse, ToolCall
from .run_store import RunStore
from .tools import build_read_only_registry
from .workspace import Workspace


class MemoryAwareDemoProvider:
    """Use injected memory when present; otherwise request the real file."""

    def __init__(self) -> None:
        self.call_number = 0

    def complete(self, messages: list[Message], tools: list[dict]) -> ModelResponse:
        del tools
        visible_text = "\n".join(message.content for message in messages)
        match = re.search(r"default_budget=(\d+)", visible_text)
        if match:
            return ModelResponse(content=f"The default budget is {match.group(1)}.")
        self.call_number += 1
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    f"demo-read-{self.call_number}",
                    "read_file",
                    {"path": "config.txt"},
                ),
            )
        )


def tool_count(result: AgentResult) -> int:
    return sum(event.kind == "tool_executed" for event in result.trace)


def main() -> None:
    demo_id = uuid.uuid4().hex[:8]
    workspace = Workspace(Path.cwd() / ".picoclaw" / f"m4-demo-{demo_id}")
    workspace.root.mkdir(parents=True, exist_ok=False)
    config_path = workspace.root / "config.txt"
    config_path.write_text("default_budget=6\n", encoding="utf-8")

    memory = MemoryManager(workspace)
    context = ContextManager(
        TokenCounter("unknown-local-model"),
        ContextBudget(max_input_tokens=512, max_memory_tokens=160, max_summary_tokens=80),
    )
    provider = MemoryAwareDemoProvider()

    def run(request: str) -> AgentResult:
        return Agent(
            provider,
            build_read_only_registry(workspace),
            run_store=RunStore(workspace.root / ".picoclaw" / "runs"),
            context_manager=context,
            memory_manager=memory,
        ).run(request)

    first = run("Read config.txt and tell me the default_budget")
    second = run("What was the default_budget in config.txt?")
    config_path.write_text("default_budget=8\n", encoding="utf-8")
    third = run("What is the current default_budget in config.txt?")

    print("=== M4 Memory Demo ===")
    print(f"1. Bootstrap: answer={first.answer!r}, tool_calls={tool_count(first)}")
    print(f"2. Memory hit: answer={second.answer!r}, tool_calls={tool_count(second)}")
    print(
        f"3. File changed: answer={third.answer!r}, tool_calls={tool_count(third)}, "
        f"invalidated={len(memory.last_invalidated)}"
    )
    print(f"\nDemo workspace: {workspace.root}")


if __name__ == "__main__":
    main()
