"""Offline M7-A demo: repository chunking, selection, and evidence injection."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from .agent import Agent
from .context_manager import ContextBudget, ContextManager, TokenCounter
from .models import Message, ModelResponse
from .run_store import RunStore
from .selection import ChunkingConfig, RepositoryEvidenceProvider
from .tools import build_read_only_registry
from .workspace import Workspace


class EvidenceAwareDemoProvider:
    """A deterministic provider proving selected evidence reached the model."""

    def complete(self, messages: list[Message], tools: list[dict]) -> ModelResponse:
        del tools
        visible = "\n".join(message.content for message in messages)
        location = re.search(r"path=([^ ]+) lines=(\d+-\d+)", visible)
        if "validate_refresh_token" in visible and location:
            return ModelResponse(
                content=(
                    "The refresh token is validated by validate_refresh_token in "
                    f"{location.group(1)}:{location.group(2)}."
                )
            )
        return ModelResponse(content="No relevant repository evidence was selected.")


def main() -> None:
    demo_root = Path.cwd() / ".picoclaw" / f"m7-demo-{uuid.uuid4().hex[:8]}"
    workspace = Workspace(demo_root)
    (workspace.root / "src").mkdir(parents=True, exist_ok=False)
    (workspace.root / "docs").mkdir()
    (workspace.root / "src" / "auth.py").write_text(
        "def validate_refresh_token(token):\n    return token.expires_at > current_time()\n",
        encoding="utf-8",
    )
    (workspace.root / "src" / "payments.py").write_text(
        "def calculate_invoice_total(items):\n    return sum(item.price for item in items)\n",
        encoding="utf-8",
    )
    (workspace.root / "docs" / "architecture.md").write_text(
        "The service uses a small modular runtime.\n",
        encoding="utf-8",
    )

    counter = TokenCounter("unknown-local-model")
    context = ContextManager(
        counter,
        ContextBudget(
            max_input_tokens=800,
            max_memory_tokens=160,
            max_summary_tokens=80,
            max_evidence_tokens=240,
        ),
    )
    evidence = RepositoryEvidenceProvider(
        workspace,
        counter,
        token_budget=240,
        chunking=ChunkingConfig(max_chunk_tokens=80),
    )
    result = Agent(
        EvidenceAwareDemoProvider(),
        build_read_only_registry(workspace),
        context_manager=context,
        evidence_provider=evidence,
        run_store=RunStore(workspace.root / ".picoclaw" / "runs"),
    ).run("Where is validate_refresh_token implemented and what does it check?")

    selected = [event for event in result.trace if event.kind == "context_chunk_selected"]
    finished = next(event for event in result.trace if event.kind == "context_selection_finished")
    print("=== M7-A Context Selection Demo ===")
    print(f"answer={result.answer!r}")
    print(f"selected_locations={[event.detail for event in selected]}")
    print(
        "selection="
        f"{finished.metadata['selector']}, "
        f"tokens={finished.metadata['selected_tokens']}/{finished.metadata['token_budget']}"
    )
    print("tool_calls=0 (the answer came from preselected evidence)")
    print(f"report={Path(result.run_directory or '') / 'report.json'}")


if __name__ == "__main__":
    main()
