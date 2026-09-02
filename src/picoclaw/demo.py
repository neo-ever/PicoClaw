"""Offline end-to-end demo for the M1 runtime."""

from __future__ import annotations

from pathlib import Path

from .agent import Agent
from .models import Message, ModelResponse, ToolCall
from .tools import build_read_only_registry
from .workspace import Workspace


class DemoProvider:
    """A tiny deterministic provider used to visualize the Agent Loop."""

    def complete(self, messages: list[Message], tools: list[dict]) -> ModelResponse:
        del tools
        tool_messages = [message for message in messages if message.role == "tool"]
        if not tool_messages:
            return ModelResponse(
                content="我需要先读取 README.md。",
                tool_calls=(ToolCall("demo-read-1", "read_file", {"path": "README.md"}),),
            )

        first_line = tool_messages[-1].content.splitlines()[0]
        return ModelResponse(content=f"读取完成，README 第一行是：{first_line}")


def main() -> None:
    workspace = Workspace(Path.cwd())
    agent = Agent(DemoProvider(), build_read_only_registry(workspace))
    result = agent.run("读取当前项目的 README，并告诉我第一行是什么。")

    print("=== Final Answer ===")
    print(result.answer)
    print("\n=== Trace ===")
    for event in result.trace:
        print(f"- {event.kind}: {event.detail}")


if __name__ == "__main__":
    main()
