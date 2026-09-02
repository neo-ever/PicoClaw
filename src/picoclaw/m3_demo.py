"""Interactive offline demonstration of M3 approval and run artifacts."""

from __future__ import annotations

import argparse
from pathlib import Path

from .agent import Agent
from .models import Message, ModelResponse, ToolCall
from .run_store import RunStore
from .tools import build_coding_registry
from .workspace import Workspace


class M3DemoProvider:
    """Choose deterministic actions while the real Runtime performs them."""

    def complete(self, messages: list[Message], tools: list[dict]) -> ModelResponse:
        del tools
        results = [message for message in messages if message.role == "tool"]
        if not results:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        "demo-write-1",
                        "write_file",
                        {"path": "generated.txt", "content": "created by PicoClaw M3\n"},
                    ),
                )
            )
        if len(results) == 1:
            if "approval denied" in results[-1].content:
                return ModelResponse(content="Write approval was denied; no file was created.")
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        "demo-shell-1",
                        "run_shell",
                        {"command": "python -m pytest --version", "timeout": 20},
                    ),
                )
            )
        if "approval denied" in results[-1].content:
            return ModelResponse(content="The file was created, but shell approval was denied.")
        return ModelResponse(content="The file was created and the allowlisted command succeeded.")


def main() -> None:
    parser = argparse.ArgumentParser(description="PicoClaw M3 offline safety demo")
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask")
    args = parser.parse_args()

    workspace = Workspace(Path.cwd() / ".picoclaw" / "m3-demo-workspace")
    workspace.root.mkdir(parents=True, exist_ok=True)
    tools = build_coding_registry(workspace, approval_mode=args.approval)
    run_store = RunStore(workspace.root / ".picoclaw" / "runs")
    agent = Agent(M3DemoProvider(), tools, max_steps=4, run_store=run_store)
    result = agent.run("Create generated.txt, then run an allowlisted verification command.")

    print("\n=== Final Answer ===")
    print(result.answer)
    print("\n=== Trace ===")
    for event in result.trace:
        print(f"- {event.kind}: {event.detail}")
    print(f"\nRun artifacts: {result.run_directory}")


if __name__ == "__main__":
    main()
