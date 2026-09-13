"""Offline M7-B demo: simulated HTTP scoring, cache hit, and fallback."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

from .agent import Agent, AgentResult
from .context_manager import ContextBudget, ContextManager
from .models import Message, ModelResponse
from .selection import (
    HttpDeltaSelector,
    HttpSelectorConfig,
    RepositoryEvidenceProvider,
)
from .tools import build_read_only_registry
from .workspace import Workspace


class SimulatedSelectorTransport:
    """Imitate the 0.8B /score_batch contract without a GPU or server."""

    def __init__(self) -> None:
        self.requests = 0

    def post_json(self, url, payload, timeout_seconds):
        del url, timeout_seconds
        self.requests += 1
        scores = []
        for item in payload["items"]:
            context = item["context"]
            score = 0.0
            if "validate_refresh_token" in context:
                score += 10.0
            if "refresh token troubleshooting" in context:
                score += 1.0
            scores.append({"score": score})
        return {"scores": scores}


class UnavailableSelectorTransport:
    def post_json(self, url, payload, timeout_seconds):
        del url, payload, timeout_seconds
        raise TimeoutError("simulated selector timeout")


class EvidenceAwareProvider:
    def complete(self, messages: list[Message], tools: list[dict]) -> ModelResponse:
        del tools
        visible = "\n".join(message.content for message in messages)
        if "validate_refresh_token" in visible:
            return ModelResponse(content="Token validation evidence came from src/auth.py.")
        return ModelResponse(content="No token validation evidence was available.")


class DemoTokenCounter:
    """Deterministic offline counter; production continues to use TokenCounter."""

    mode = "demo:four-chars-per-token"

    @staticmethod
    def count_text(text: str) -> int:
        return (len(text) + 3) // 4

    @staticmethod
    def truncate_text(text: str, limit: int) -> str:
        return text[: limit * 4]

    def count_messages(self, messages: Sequence[Message]) -> int:
        return sum(self.count_text(message.content) + 4 for message in messages)


def selection_metadata(result: AgentResult) -> dict:
    event = next(item for item in result.trace if item.kind == "context_selection_finished")
    return event.metadata


def run_agent(
    workspace: Workspace,
    context: ContextManager,
    evidence: RepositoryEvidenceProvider,
) -> AgentResult:
    return Agent(
        EvidenceAwareProvider(),
        build_read_only_registry(workspace),
        context_manager=context,
        evidence_provider=evidence,
    ).run("Where does validate_refresh_token check a refresh token?")


def main() -> None:
    workspace = Workspace(Path.cwd() / ".picoclaw" / f"m7b-demo-{uuid.uuid4().hex[:8]}")
    (workspace.root / "src").mkdir(parents=True, exist_ok=False)
    (workspace.root / "docs").mkdir()
    (workspace.root / "src" / "auth.py").write_text(
        "def validate_refresh_token(token):\n    return token.is_valid\n",
        encoding="utf-8",
    )
    (workspace.root / "docs" / "auth.md").write_text(
        "refresh token troubleshooting and common validation errors\n",
        encoding="utf-8",
    )
    (workspace.root / "src" / "payment.py").write_text(
        "def calculate_invoice_total(items):\n    return sum(items)\n",
        encoding="utf-8",
    )

    counter = DemoTokenCounter()
    context = ContextManager(
        counter,
        ContextBudget(
            max_input_tokens=800,
            max_memory_tokens=160,
            max_summary_tokens=80,
            max_evidence_tokens=240,
        ),
    )
    transport = SimulatedSelectorTransport()
    http_selector = HttpDeltaSelector(
        HttpSelectorConfig(batch_size=8, candidate_pool_size=8),
        transport=transport,
    )
    evidence = RepositoryEvidenceProvider(
        workspace,
        counter,
        selector=http_selector,
        token_budget=240,
    )

    first = run_agent(workspace, context, evidence)
    second = run_agent(workspace, context, evidence)

    fallback_evidence = RepositoryEvidenceProvider(
        workspace,
        counter,
        selector=HttpDeltaSelector(transport=UnavailableSelectorTransport()),
        token_budget=240,
    )
    fallback = run_agent(workspace, context, fallback_evidence)
    first_meta = selection_metadata(first)
    second_meta = selection_metadata(second)
    fallback_meta = selection_metadata(fallback)

    print("=== M7-B HTTP Selector Demo ===")
    print(
        "1. Simulated HTTP: "
        f"requests={first_meta['http_requests']}, "
        f"cache_misses={first_meta['cache_misses']}, answer={first.answer!r}"
    )
    print(
        "2. Same request: "
        f"requests={second_meta['http_requests']}, "
        f"cache_hits={second_meta['cache_hits']}, answer={second.answer!r}"
    )
    print(
        "3. Timeout fallback: "
        f"fallback={fallback_meta['fallback_used']}, "
        f"reason={fallback_meta['fallback_reason']}, answer={fallback.answer!r}"
    )
    print(f"demo_workspace={workspace.root}")


if __name__ == "__main__":
    main()
