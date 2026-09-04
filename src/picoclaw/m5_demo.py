"""Offline demonstration of progressive Skills and controlled MCP tools."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from .agent import Agent
from .context_manager import ContextBudget, ContextManager, TokenCounter
from .mcp_adapter import MCPServerConfig, MCPToolAdapter
from .models import Message, ModelResponse, ToolCall
from .run_store import RunStore
from .skills import SkillCatalog
from .tools import ApprovalMode, Approver, ToolRegistry

DEMO_SKILL = """---
name: repository-summary
description: Inspect and summarize a local code repository with evidence from important files.
---

Read the README and package manifest before summarizing a repository.
Use only Runtime-approved tools and distinguish verified facts from inference.
"""


class M5DemoProvider:
    def complete(self, messages: list[Message], tools: list[dict]) -> ModelResponse:
        tool_messages = [message for message in messages if message.role == "tool"]
        if not tool_messages:
            tool_name = next(
                item["function"]["name"]
                for item in tools
                if item["function"]["name"].endswith("__word_count")
            )
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        "m5-count-1",
                        tool_name,
                        {"text": "PicoClaw loads only the skill selected for this task"},
                    ),
                )
            )
        skill_visible = any(
            message.role == "system" and "repository-summary" in message.content
            for message in messages
        )
        return ModelResponse(
            content=(
                f"MCP counted {tool_messages[-1].content} words; "
                f"selected skill visible={str(skill_visible).lower()}."
            )
        )


def main() -> None:
    project_root = Path.cwd().resolve()
    demo_root = project_root / ".picoclaw" / f"m5-demo-{uuid.uuid4().hex[:8]}"
    skill_root = demo_root / "skills"
    skill_directory = skill_root / "repository-summary"
    skill_directory.mkdir(parents=True, exist_ok=False)
    (skill_directory / "SKILL.md").write_text(DEMO_SKILL, encoding="utf-8")
    registry = ToolRegistry(Approver(ApprovalMode.AUTO))
    adapter = MCPToolAdapter(
        MCPServerConfig(
            name="local-demo",
            command=sys.executable,
            args=("-m", "picoclaw.mcp_demo_server"),
            cwd=project_root,
            allowed_tools=frozenset({"word_count"}),
        )
    )
    descriptors = adapter.register_tools(registry)
    catalog = SkillCatalog([skill_root])
    context = ContextManager(
        TokenCounter("unknown-local-model"),
        ContextBudget(
            max_input_tokens=1_024,
            max_skill_tokens=300,
            max_memory_tokens=200,
            max_summary_tokens=100,
        ),
    )
    result = Agent(
        M5DemoProvider(),
        registry,
        run_store=RunStore(demo_root / "runs"),
        context_manager=context,
        skill_catalog=catalog,
    ).run("Use the repository summary skill and count the words in its example sentence")

    selection = next(event for event in result.trace if event.kind == "skills_selected")
    print("=== M5 Skills + MCP Demo ===")
    print(f"Skill catalog size: {selection.metadata['catalog_size']}")
    print(f"Skills loaded: {selection.metadata['selected']}")
    print(f"MCP tools registered: {[item.exposed_name for item in descriptors]}")
    print(f"Final answer: {result.answer}")
    print(f"Run report: {Path(result.run_directory or '') / 'report.json'}")


if __name__ == "__main__":
    main()
