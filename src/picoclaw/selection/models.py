"""Provider-independent models for repository evidence selection."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContextChunk:
    """One bounded, traceable excerpt from a repository file."""

    id: str
    path: str
    start_line: int
    end_line: int
    content: str
    source_sha256: str
    token_count: int

    def __post_init__(self) -> None:
        if not self.id or not self.path:
            raise ValueError("chunk id and path must not be empty")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("chunk line range is invalid")
        if not self.content:
            raise ValueError("chunk content must not be empty")
        if self.token_count < 1:
            raise ValueError("chunk token count must be positive")

    @property
    def location(self) -> str:
        return f"{self.path}:{self.start_line}-{self.end_line}"

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ScoredChunk:
    """A selected chunk plus the scores that explain the decision."""

    chunk: ContextChunk
    score: float
    relevance_score: float
    redundancy: float = 0.0


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """Auditable output shared by lexical and learned selectors."""

    selector: str
    candidates: int
    selected: tuple[ScoredChunk, ...]
    token_budget: int
    selected_tokens: int
    elapsed_ms: int
    cache_hits: int = 0
    cache_misses: int = 0
    http_requests: int = 0
    fallback_used: bool = False
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.selector:
            raise ValueError("selector identity must not be empty")
        if (
            min(
                self.candidates,
                self.token_budget,
                self.selected_tokens,
                self.elapsed_ms,
                self.cache_hits,
                self.cache_misses,
                self.http_requests,
            )
            < 0
        ):
            raise ValueError("selection counters cannot be negative")
        if self.selected_tokens > self.token_budget:
            raise ValueError("selected chunks exceed the token budget")
        if self.selected_tokens != sum(item.chunk.token_count for item in self.selected):
            raise ValueError("selected token total does not match selected chunks")
        if self.fallback_used != (self.fallback_reason is not None):
            raise ValueError("fallback reason must match fallback usage")

    @property
    def chunks(self) -> tuple[ContextChunk, ...]:
        return tuple(item.chunk for item in self.selected)
