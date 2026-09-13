"""A dependency-free BM25-style selector with duplicate suppression."""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from collections.abc import Sequence
from typing import ClassVar

from .models import ContextChunk, ScoredChunk, SelectionResult


class LexicalSelector:
    """Select relevant chunks locally, cheaply, and deterministically."""

    name = "lexical-bm25-diversity-v1"
    _WORD_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[\u4e00-\u9fff]+")
    _CAMEL_PATTERN = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
    _STOP_WORDS: ClassVar[set[str]] = {
        "a",
        "an",
        "and",
        "are",
        "for",
        "in",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    }

    def __init__(self, *, redundancy_penalty: float = 0.45, path_boost: float = 0.35) -> None:
        if not 0 <= redundancy_penalty <= 1:
            raise ValueError("redundancy_penalty must be between 0 and 1")
        if path_boost < 0:
            raise ValueError("path_boost cannot be negative")
        self.redundancy_penalty = redundancy_penalty
        self.path_boost = path_boost

    def identity_payload(self) -> dict[str, float | str]:
        return {
            "type": self.name,
            "redundancy_penalty": self.redundancy_penalty,
            "path_boost": self.path_boost,
        }

    def select(
        self,
        query: str,
        candidates: Sequence[ContextChunk],
        token_budget: int,
    ) -> SelectionResult:
        if token_budget < 0:
            raise ValueError("token_budget cannot be negative")
        started_at = time.perf_counter()
        query_terms = self._terms(query)
        if not query_terms or not candidates or token_budget == 0:
            return self._empty_result(len(candidates), token_budget, started_at)

        document_terms = [self._terms(f"{chunk.path}\n{chunk.content}") for chunk in candidates]
        document_frequencies: Counter[str] = Counter()
        for terms in document_terms:
            document_frequencies.update(set(terms))
        average_length = sum(len(terms) for terms in document_terms) / len(document_terms)
        query_counts = Counter(query_terms)

        ranked: list[tuple[ContextChunk, float, set[str]]] = []
        for chunk, terms in zip(candidates, document_terms, strict=True):
            relevance = self._bm25(
                terms,
                query_counts,
                document_frequencies,
                len(candidates),
                average_length,
            )
            path_terms = set(self._terms(chunk.path))
            path_overlap = len(path_terms.intersection(query_counts))
            relevance += self.path_boost * path_overlap
            if relevance > 0:
                ranked.append((chunk, relevance, set(terms)))

        selected: list[ScoredChunk] = []
        selected_terms: list[set[str]] = []
        selected_content_hashes: set[str] = set()
        used_tokens = 0
        remaining = ranked.copy()
        while remaining:
            best_index = -1
            best_key: tuple[float, float, str] | None = None
            best_redundancy = 0.0
            for index, (chunk, relevance, terms) in enumerate(remaining):
                if used_tokens + chunk.token_count > token_budget:
                    continue
                content_hash = _content_digest(chunk.content)
                if content_hash in selected_content_hashes:
                    continue
                redundancy = max(
                    (self._jaccard(terms, prior) for prior in selected_terms),
                    default=0.0,
                )
                adjusted = relevance * (1 - self.redundancy_penalty * redundancy)
                key = (adjusted, relevance, chunk.id)
                if best_key is None or key > best_key:
                    best_index = index
                    best_key = key
                    best_redundancy = redundancy
            if best_index < 0 or best_key is None or best_key[0] <= 0:
                break

            chunk, relevance, terms = remaining.pop(best_index)
            selected.append(
                ScoredChunk(
                    chunk=chunk,
                    score=best_key[0],
                    relevance_score=relevance,
                    redundancy=best_redundancy,
                )
            )
            selected_terms.append(terms)
            selected_content_hashes.add(_content_digest(chunk.content))
            used_tokens += chunk.token_count

        return SelectionResult(
            selector=self.name,
            candidates=len(candidates),
            selected=tuple(selected),
            token_budget=token_budget,
            selected_tokens=used_tokens,
            elapsed_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
        )

    def _bm25(
        self,
        terms: list[str],
        query_counts: Counter[str],
        document_frequencies: Counter[str],
        document_count: int,
        average_length: float,
    ) -> float:
        frequencies = Counter(terms)
        length = len(terms)
        k1 = 1.5
        b = 0.75
        score = 0.0
        for term, query_frequency in query_counts.items():
            frequency = frequencies[term]
            if frequency == 0:
                continue
            document_frequency = document_frequencies[term]
            inverse_frequency = math.log(
                1 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            denominator = frequency + k1 * (1 - b + b * length / max(average_length, 1.0))
            score += inverse_frequency * frequency * (k1 + 1) / denominator * query_frequency
        return score

    @classmethod
    def _terms(cls, text: str) -> list[str]:
        terms: list[str] = []
        for match in cls._WORD_PATTERN.findall(text):
            if match.isascii():
                lowered = match.lower()
                if lowered not in cls._STOP_WORDS:
                    terms.append(lowered)
                for part in cls._CAMEL_PATTERN.findall(match.replace("_", " ")):
                    part = part.lower()
                    if len(part) > 1 and part not in cls._STOP_WORDS and part != lowered:
                        terms.append(part)
                continue
            terms.extend(match)
            terms.extend(match[index : index + 2] for index in range(len(match) - 1))
        return terms

    @staticmethod
    def _jaccard(left: set[str], right: set[str]) -> float:
        union = left | right
        return len(left & right) / len(union) if union else 0.0

    def _empty_result(
        self,
        candidate_count: int,
        token_budget: int,
        started_at: float,
    ) -> SelectionResult:
        return SelectionResult(
            selector=self.name,
            candidates=candidate_count,
            selected=(),
            token_budget=token_budget,
            selected_tokens=0,
            elapsed_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
        )


def _content_digest(content: str) -> str:
    """Keep exact duplicate suppression independent from chunk identifiers."""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()
