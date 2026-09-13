"""Composition layer from a safe repository scan to selected evidence."""

from __future__ import annotations

from dataclasses import asdict

from ..workspace import Workspace
from .chunker import ChunkingConfig, ChunkTokenCounter, RepositoryChunker
from .lexical import LexicalSelector
from .models import SelectionResult
from .protocol import ContextSelector


class RepositoryEvidenceProvider:
    """Build candidates locally, then delegate the ranking policy."""

    def __init__(
        self,
        workspace: Workspace,
        token_counter: ChunkTokenCounter,
        *,
        selector: ContextSelector | None = None,
        token_budget: int = 1_200,
        chunking: ChunkingConfig | None = None,
    ) -> None:
        if token_budget < 0:
            raise ValueError("token_budget cannot be negative")
        self.chunker = RepositoryChunker(workspace, token_counter, chunking)
        self.selector = selector if selector is not None else LexicalSelector()
        self.token_budget = token_budget

    def select(self, query: str) -> SelectionResult:
        candidates = self.chunker.chunk_repository()
        return self.selector.select(query, candidates, self.token_budget)

    def identity_payload(self) -> dict:
        selector_identity = getattr(self.selector, "identity_payload", None)
        return {
            "type": f"{type(self).__module__}.{type(self).__qualname__}",
            "token_budget": self.token_budget,
            "chunking": asdict(self.chunker.config),
            "selector": (
                selector_identity()
                if callable(selector_identity)
                else f"{type(self.selector).__module__}.{type(self.selector).__qualname__}"
            ),
        }
