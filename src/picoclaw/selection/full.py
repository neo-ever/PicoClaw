"""Deterministic full-context baseline for controlled comparisons."""

from __future__ import annotations

import time
from collections.abc import Sequence

from .models import ContextChunk, ScoredChunk, SelectionResult


class FullContextSelector:
    """Take repository chunks in stable order until the evidence budget is full."""

    name = "full-context-v1"

    @staticmethod
    def identity_payload() -> dict[str, str]:
        return {"type": FullContextSelector.name}

    def select(
        self,
        query: str,
        candidates: Sequence[ContextChunk],
        token_budget: int,
    ) -> SelectionResult:
        del query
        if token_budget < 0:
            raise ValueError("token_budget cannot be negative")
        started_at = time.perf_counter()
        selected: list[ScoredChunk] = []
        selected_tokens = 0
        for chunk in candidates:
            if selected_tokens + chunk.token_count > token_budget:
                continue
            selected.append(ScoredChunk(chunk, score=0.0, relevance_score=0.0))
            selected_tokens += chunk.token_count
        return SelectionResult(
            selector=self.name,
            candidates=len(candidates),
            selected=tuple(selected),
            token_budget=token_budget,
            selected_tokens=selected_tokens,
            elapsed_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
        )
