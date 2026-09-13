"""HTTP-backed greedy delta selection with batching, caching, and fallback."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from .cache import ScoreCache
from .lexical import LexicalSelector
from .models import ContextChunk, ScoredChunk, SelectionResult
from .protocol import ContextSelector


class SelectorServiceError(RuntimeError):
    """The remote scoring service was unavailable or returned invalid data."""


class SelectorResponseError(SelectorServiceError):
    """The scoring response did not match the documented contract."""


class JsonTransport(Protocol):
    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        """POST JSON and return a decoded object."""


class UrllibJsonTransport:
    """Bounded standard-library JSON transport with normalized errors."""

    def __init__(self, max_response_bytes: int = 1_000_000) -> None:
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        self.max_response_bytes = max_response_bytes

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise SelectorServiceError(f"selector HTTP status {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SelectorServiceError("selector service connection failed") from exc
        if len(raw) > self.max_response_bytes:
            raise SelectorResponseError("selector response exceeds byte limit")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SelectorResponseError("selector response is not valid UTF-8 JSON") from exc
        if not isinstance(decoded, dict):
            raise SelectorResponseError("selector response must be a JSON object")
        return decoded


@dataclass(frozen=True, slots=True)
class HttpSelectorConfig:
    base_url: str = "http://127.0.0.1:6006"
    timeout_seconds: float = 8.0
    batch_size: int = 8
    cache_entries: int = 2_048
    candidate_pool_size: int = 24
    min_delta: float = 0.0
    allow_remote: bool = False

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("selector base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("selector base_url cannot contain credentials, query, or fragment")
        if self.timeout_seconds <= 0:
            raise ValueError("selector timeout must be positive")
        if min(self.batch_size, self.cache_entries, self.candidate_pool_size) < 1:
            raise ValueError("selector batch, cache, and candidate limits must be positive")
        if not math.isfinite(self.min_delta):
            raise ValueError("selector min_delta must be finite")
        if not self.allow_remote and not _is_loopback_host(parsed.hostname):
            raise ValueError(
                "remote selector URL requires allow_remote=True because repository code is sent"
            )

    @property
    def score_batch_url(self) -> str:
        return self.base_url.rstrip("/") + "/score_batch"

    @property
    def predict_delta_batch_url(self) -> str:
        return self.base_url.rstrip("/") + "/predict_delta_batch"


@dataclass(slots=True)
class _CallStats:
    cache_hits: int = 0
    cache_misses: int = 0
    http_requests: int = 0


class HttpDeltaSelector:
    """Greedily add chunks whose remote score has positive marginal value."""

    name = "http-delta-qwen0.8b-v1"
    scoring_contract = "p(question|context)"

    def __init__(
        self,
        config: HttpSelectorConfig | None = None,
        *,
        fallback: ContextSelector | None = None,
        transport: JsonTransport | None = None,
        cache: ScoreCache | None = None,
    ) -> None:
        self.config = config or HttpSelectorConfig()
        self.fallback = fallback if fallback is not None else LexicalSelector()
        self.transport = transport if transport is not None else UrllibJsonTransport()
        self.cache = cache if cache is not None else ScoreCache(self.config.cache_entries)

    def identity_payload(self) -> dict[str, Any]:
        fallback_identity = getattr(self.fallback, "identity_payload", None)
        return {
            "type": self.name,
            "scoring": self.scoring_contract,
            "config": asdict(self.config),
            "fallback": (
                fallback_identity()
                if callable(fallback_identity)
                else f"{type(self.fallback).__module__}.{type(self.fallback).__qualname__}"
            ),
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
        stats = _CallStats()
        if not query.strip() or not candidates or token_budget == 0:
            return self._result((), len(candidates), token_budget, started_at, stats)

        pool_budget = sum(chunk.token_count for chunk in candidates)
        lexical_pool = self.fallback.select(query, candidates, pool_budget)
        pool = list(lexical_pool.selected[: self.config.candidate_pool_size])
        if not pool:
            return self._result((), len(candidates), token_budget, started_at, stats)

        try:
            selected = self._greedy_select(query, pool, token_budget, stats)
        except (SelectorServiceError, TimeoutError, OSError) as exc:
            return self._fallback_result(
                query,
                candidates,
                token_budget,
                type(exc).__name__,
                started_at,
                stats,
            )
        if not selected:
            return self._fallback_result(
                query,
                candidates,
                token_budget,
                "no_positive_delta",
                started_at,
                stats,
            )
        return self._result(
            tuple(selected),
            len(candidates),
            token_budget,
            started_at,
            stats,
        )

    def _greedy_select(
        self,
        query: str,
        pool: list[ScoredChunk],
        token_budget: int,
        stats: _CallStats,
    ) -> list[ScoredChunk]:
        selected: list[ScoredChunk] = []
        remaining = pool.copy()
        selected_tokens = 0
        selected_chunks: list[ContextChunk] = []

        while remaining:
            eligible = [
                item
                for item in remaining
                if selected_tokens + item.chunk.token_count <= token_budget
            ]
            if not eligible:
                break
            base_context = self._render_context(selected_chunks)
            plus_contexts = [
                self._render_context([*selected_chunks, item.chunk]) for item in eligible
            ]
            scores = self._score_contexts(query, [base_context, *plus_contexts], stats)
            base_score = scores[0]
            ranked = [(scores[index + 1] - base_score, item) for index, item in enumerate(eligible)]
            delta, best = max(
                ranked,
                key=lambda pair: (pair[0], pair[1].relevance_score, pair[1].chunk.id),
            )
            if delta <= self.config.min_delta:
                break
            selected.append(
                ScoredChunk(
                    chunk=best.chunk,
                    score=delta,
                    relevance_score=best.relevance_score,
                    redundancy=best.redundancy,
                )
            )
            selected_chunks.append(best.chunk)
            selected_tokens += best.chunk.token_count
            remaining.remove(best)
        return selected

    def _score_contexts(
        self,
        query: str,
        contexts: Sequence[str],
        stats: _CallStats,
    ) -> list[float]:
        scores: list[float | None] = [None] * len(contexts)
        pending: dict[str, tuple[str, list[int]]] = {}
        for index, context in enumerate(contexts):
            key = self._cache_key(query, context)
            hit, value = self.cache.get(key)
            if hit:
                stats.cache_hits += 1
                scores[index] = value
                continue
            if key in pending:
                pending[key][1].append(index)
                continue
            pending[key] = (context, [index])

        pending_items = list(pending.items())
        stats.cache_misses += len(pending_items)
        for start in range(0, len(pending_items), self.config.batch_size):
            batch = pending_items[start : start + self.config.batch_size]
            payload = {
                "items": [{"context": context, "question": query} for _, (context, _) in batch]
            }
            stats.http_requests += 1
            response = self.transport.post_json(
                self.config.score_batch_url,
                payload,
                self.config.timeout_seconds,
            )
            parsed_scores = self._parse_scores(response, len(batch))
            for (key, (_, indices)), score in zip(batch, parsed_scores, strict=True):
                self.cache.put(key, score)
                for index in indices:
                    scores[index] = score

        if any(score is None for score in scores):
            raise SelectorResponseError("selector did not return every requested score")
        return [float(score) for score in scores]

    @staticmethod
    def _parse_scores(response: Mapping[str, Any], expected: int) -> list[float]:
        raw_scores = response.get("scores")
        if not isinstance(raw_scores, list) or len(raw_scores) != expected:
            raise SelectorResponseError("selector scores length does not match request")
        scores: list[float] = []
        for item in raw_scores:
            raw_score = item.get("score") if isinstance(item, dict) else item
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                raise SelectorResponseError("selector score must be numeric")
            score = float(raw_score)
            if not math.isfinite(score):
                raise SelectorResponseError("selector score must be finite")
            scores.append(score)
        return scores

    def _fallback_result(
        self,
        query: str,
        candidates: Sequence[ContextChunk],
        token_budget: int,
        reason: str,
        started_at: float,
        stats: _CallStats,
    ) -> SelectionResult:
        fallback = self.fallback.select(query, candidates, token_budget)
        return SelectionResult(
            selector=f"{self.name}->fallback:{fallback.selector}",
            candidates=len(candidates),
            selected=fallback.selected,
            token_budget=token_budget,
            selected_tokens=fallback.selected_tokens,
            elapsed_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
            cache_hits=stats.cache_hits,
            cache_misses=stats.cache_misses,
            http_requests=stats.http_requests,
            fallback_used=True,
            fallback_reason=reason,
        )

    def _result(
        self,
        selected: tuple[ScoredChunk, ...],
        candidate_count: int,
        token_budget: int,
        started_at: float,
        stats: _CallStats,
    ) -> SelectionResult:
        return SelectionResult(
            selector=self.name,
            candidates=candidate_count,
            selected=selected,
            token_budget=token_budget,
            selected_tokens=sum(item.chunk.token_count for item in selected),
            elapsed_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
            cache_hits=stats.cache_hits,
            cache_misses=stats.cache_misses,
            http_requests=stats.http_requests,
        )

    @staticmethod
    def _cache_key(query: str, context: str) -> str:
        payload = f"{query}\0{context}".encode()
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _render_context(chunks: Sequence[ContextChunk]) -> str:
        return "\n\n".join(
            (f"[file={chunk.path} lines={chunk.start_line}-{chunk.end_line}]\n{chunk.content}")
            for chunk in chunks
        )


class DirectDeltaHttpSelector(HttpDeltaSelector):
    """Use a model trained to predict verifier delta from minus/plus evidence."""

    name = "http-direct-delta-qwen0.8b-v1"
    scoring_contract = "verifier_delta(minus_context,plus_context,question)"

    def _greedy_select(
        self,
        query: str,
        pool: list[ScoredChunk],
        token_budget: int,
        stats: _CallStats,
    ) -> list[ScoredChunk]:
        selected: list[ScoredChunk] = []
        remaining = pool.copy()
        selected_tokens = 0
        selected_chunks: list[ContextChunk] = []
        while remaining:
            eligible = [
                item
                for item in remaining
                if selected_tokens + item.chunk.token_count <= token_budget
            ]
            if not eligible:
                break
            minus_context = self._render_context(selected_chunks)
            candidate_contexts = [self._render_context([item.chunk]) for item in eligible]
            plus_contexts = [
                self._render_context([*selected_chunks, item.chunk]) for item in eligible
            ]
            deltas = self._predict_deltas(
                query,
                minus_context,
                candidate_contexts,
                plus_contexts,
                stats,
            )
            delta, best = max(
                zip(deltas, eligible, strict=True),
                key=lambda pair: (pair[0], pair[1].relevance_score, pair[1].chunk.id),
            )
            if delta <= self.config.min_delta:
                break
            selected.append(ScoredChunk(best.chunk, delta, best.relevance_score, best.redundancy))
            selected_chunks.append(best.chunk)
            selected_tokens += best.chunk.token_count
            remaining.remove(best)
        return selected

    def _predict_deltas(
        self,
        query: str,
        minus_context: str,
        candidate_contexts: Sequence[str],
        plus_contexts: Sequence[str],
        stats: _CallStats,
    ) -> list[float]:
        if len(candidate_contexts) != len(plus_contexts):
            raise ValueError("candidate and plus context counts must match")
        deltas: list[float | None] = [None] * len(plus_contexts)
        pending: dict[str, tuple[str, list[int]]] = {}
        for index, plus_context in enumerate(plus_contexts):
            key = self._delta_cache_key(query, minus_context, plus_context)
            hit, value = self.cache.get(key)
            if hit:
                stats.cache_hits += 1
                deltas[index] = value
                continue
            if key in pending:
                pending[key][1].append(index)
                continue
            pending[key] = (plus_context, [index])

        pending_items = list(pending.items())
        stats.cache_misses += len(pending_items)
        for start in range(0, len(pending_items), self.config.batch_size):
            batch = pending_items[start : start + self.config.batch_size]
            payload = {
                "items": [
                    {
                        "minus_context": minus_context,
                        "candidate_context": candidate_contexts[indices[0]],
                        "plus_context": plus_context,
                        "question": query,
                    }
                    for _, (plus_context, indices) in batch
                ]
            }
            stats.http_requests += 1
            response = self.transport.post_json(
                self.config.predict_delta_batch_url,
                payload,
                self.config.timeout_seconds,
            )
            parsed = self._parse_deltas(response, len(batch))
            for (key, (_, indices)), delta in zip(batch, parsed, strict=True):
                self.cache.put(key, delta)
                for index in indices:
                    deltas[index] = delta
        if any(delta is None for delta in deltas):
            raise SelectorResponseError("selector did not return every requested delta")
        return [float(delta) for delta in deltas]

    @staticmethod
    def _parse_deltas(response: Mapping[str, Any], expected: int) -> list[float]:
        raw_deltas = response.get("deltas")
        if not isinstance(raw_deltas, list) or len(raw_deltas) != expected:
            raise SelectorResponseError("selector deltas length does not match request")
        deltas: list[float] = []
        for item in raw_deltas:
            raw_delta = item.get("delta") if isinstance(item, dict) else item
            if isinstance(raw_delta, bool) or not isinstance(raw_delta, (int, float)):
                raise SelectorResponseError("selector delta must be numeric")
            delta = float(raw_delta)
            if not math.isfinite(delta):
                raise SelectorResponseError("selector delta must be finite")
            deltas.append(delta)
        return deltas

    @staticmethod
    def _delta_cache_key(query: str, minus_context: str, plus_context: str) -> str:
        payload = f"direct-delta\0{query}\0{minus_context}\0{plus_context}".encode()
        return hashlib.sha256(payload).hexdigest()


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
