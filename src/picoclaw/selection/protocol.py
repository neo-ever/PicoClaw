"""Small interfaces that keep selection models replaceable."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .models import ContextChunk, SelectionResult


class ContextSelector(Protocol):
    """Choose useful evidence without receiving tool-execution authority."""

    name: str

    def select(
        self,
        query: str,
        candidates: Sequence[ContextChunk],
        token_budget: int,
    ) -> SelectionResult:
        """Return a budget-bounded, explainable subset of candidates."""


class EvidenceProvider(Protocol):
    """Build repository candidates and select evidence for one request."""

    def select(self, query: str) -> SelectionResult:
        """Return evidence for the current request."""
