"""A tiny trainable delta baseline that runs without PyTorch."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from .dataset import DeltaExample, render_chunks
from .lexical import LexicalSelector
from .models import ContextChunk, ScoredChunk, SelectionResult
from .protocol import ContextSelector

_TERM_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[\u4e00-\u9fff]")
_FEATURE_NAMES = (
    "query_candidate_coverage",
    "query_plus_coverage",
    "candidate_novelty",
    "candidate_redundancy",
    "candidate_log_length",
)


@dataclass(frozen=True, slots=True)
class DeltaMetrics:
    examples: int
    sign_accuracy: float
    mae: float
    rmse: float
    pearson: float


@dataclass(frozen=True, slots=True)
class LinearDeltaModel:
    feature_names: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    weights: tuple[float, ...]
    intercept: float

    def __post_init__(self) -> None:
        size = len(self.feature_names)
        if size == 0 or any(
            len(values) != size for values in (self.means, self.scales, self.weights)
        ):
            raise ValueError("linear delta model dimensions do not match")
        if any(scale <= 0 for scale in self.scales):
            raise ValueError("linear delta feature scales must be positive")
        if not all(
            math.isfinite(value)
            for value in (*self.means, *self.scales, *self.weights, self.intercept)
        ):
            raise ValueError("linear delta model values must be finite")

    def predict(self, query: str, minus_context: str, candidate_context: str) -> float:
        features = delta_features(query, minus_context, candidate_context)
        normalized = [
            (value - mean) / scale
            for value, mean, scale in zip(features, self.means, self.scales, strict=True)
        ]
        return self.intercept + sum(
            weight * value for weight, value in zip(self.weights, normalized, strict=True)
        )

    def save(self, path: Path) -> Path:
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return path

    @classmethod
    def load(cls, path: Path) -> LinearDeltaModel:
        payload = json.loads(path.resolve().read_text(encoding="utf-8"))
        return cls(
            feature_names=tuple(payload["feature_names"]),
            means=tuple(float(value) for value in payload["means"]),
            scales=tuple(float(value) for value in payload["scales"]),
            weights=tuple(float(value) for value in payload["weights"]),
            intercept=float(payload["intercept"]),
        )


def train_linear_delta(
    examples: Sequence[DeltaExample],
    *,
    epochs: int = 800,
    learning_rate: float = 0.05,
    l2: float = 0.001,
) -> LinearDeltaModel:
    if not examples:
        raise ValueError("linear delta training requires examples")
    if epochs < 1 or learning_rate <= 0 or l2 < 0:
        raise ValueError("invalid linear delta training hyperparameters")
    rows = [
        delta_features(example.query, example.minus_context, example.candidate_context)
        for example in examples
    ]
    columns = list(zip(*rows, strict=True))
    means = tuple(sum(column) / len(column) for column in columns)
    scales = tuple(
        max(
            math.sqrt(sum((value - mean) ** 2 for value in column) / len(column)),
            1e-6,
        )
        for column, mean in zip(columns, means, strict=True)
    )
    normalized = [
        [(value - mean) / scale for value, mean, scale in zip(row, means, scales, strict=True)]
        for row in rows
    ]
    targets = [example.delta for example in examples]
    weights = [0.0] * len(_FEATURE_NAMES)
    intercept = sum(targets) / len(targets)
    for _ in range(epochs):
        predictions = [
            intercept + sum(weight * value for weight, value in zip(weights, row, strict=True))
            for row in normalized
        ]
        errors = [
            prediction - target for prediction, target in zip(predictions, targets, strict=True)
        ]
        intercept_gradient = 2 * sum(errors) / len(errors)
        gradients = [
            2
            * sum(error * row[index] for error, row in zip(errors, normalized, strict=True))
            / len(errors)
            + 2 * l2 * weights[index]
            for index in range(len(weights))
        ]
        intercept -= learning_rate * intercept_gradient
        weights = [
            weight - learning_rate * gradient
            for weight, gradient in zip(weights, gradients, strict=True)
        ]
    return LinearDeltaModel(
        feature_names=_FEATURE_NAMES,
        means=means,
        scales=scales,
        weights=tuple(weights),
        intercept=intercept,
    )


def evaluate_linear_delta(
    model: LinearDeltaModel,
    examples: Sequence[DeltaExample],
) -> DeltaMetrics:
    if not examples:
        raise ValueError("linear delta evaluation requires examples")
    targets = [example.delta for example in examples]
    predictions = [
        model.predict(example.query, example.minus_context, example.candidate_context)
        for example in examples
    ]
    errors = [prediction - target for prediction, target in zip(predictions, targets, strict=True)]
    sign_accuracy = sum(
        (prediction > 0) == (target > 0)
        for prediction, target in zip(predictions, targets, strict=True)
    ) / len(targets)
    return DeltaMetrics(
        examples=len(examples),
        sign_accuracy=sign_accuracy,
        mae=sum(abs(error) for error in errors) / len(errors),
        rmse=math.sqrt(sum(error**2 for error in errors) / len(errors)),
        pearson=_pearson(predictions, targets),
    )


class LinearDeltaSelector:
    name = "linear-direct-delta-v1"

    def __init__(
        self,
        model: LinearDeltaModel,
        *,
        candidate_pool_size: int = 24,
        min_delta: float = 0.0,
        fallback: ContextSelector | None = None,
    ) -> None:
        if candidate_pool_size < 1 or not math.isfinite(min_delta):
            raise ValueError("invalid linear selector configuration")
        self.model = model
        self.candidate_pool_size = candidate_pool_size
        self.min_delta = min_delta
        self.fallback = fallback if fallback is not None else LexicalSelector()

    def identity_payload(self) -> dict:
        payload = json.dumps(asdict(self.model), sort_keys=True, separators=(",", ":"))
        return {
            "type": self.name,
            "model_sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "candidate_pool_size": self.candidate_pool_size,
            "min_delta": self.min_delta,
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
        pool_result = self.fallback.select(
            query,
            candidates,
            sum(chunk.token_count for chunk in candidates),
        )
        remaining = list(pool_result.selected[: self.candidate_pool_size])
        selected: list[ScoredChunk] = []
        selected_chunks: list[ContextChunk] = []
        selected_tokens = 0
        while remaining:
            eligible = [
                item
                for item in remaining
                if selected_tokens + item.chunk.token_count <= token_budget
            ]
            if not eligible:
                break
            minus = render_chunks(selected_chunks)
            scored = [
                (
                    self.model.predict(query, minus, render_chunks([item.chunk])),
                    item,
                )
                for item in eligible
            ]
            delta, best = max(
                scored,
                key=lambda pair: (pair[0], pair[1].relevance_score, pair[1].chunk.id),
            )
            if delta <= self.min_delta:
                break
            selected.append(ScoredChunk(best.chunk, delta, best.relevance_score, best.redundancy))
            selected_chunks.append(best.chunk)
            selected_tokens += best.chunk.token_count
            remaining.remove(best)
        if not selected:
            fallback = self.fallback.select(query, candidates, token_budget)
            return SelectionResult(
                selector=f"{self.name}->fallback:{fallback.selector}",
                candidates=len(candidates),
                selected=fallback.selected,
                token_budget=token_budget,
                selected_tokens=fallback.selected_tokens,
                elapsed_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
                fallback_used=True,
                fallback_reason="no_positive_delta",
            )
        return SelectionResult(
            selector=self.name,
            candidates=len(candidates),
            selected=tuple(selected),
            token_budget=token_budget,
            selected_tokens=selected_tokens,
            elapsed_ms=max(0, int((time.perf_counter() - started_at) * 1000)),
        )


def delta_features(query: str, minus_context: str, candidate_context: str) -> tuple[float, ...]:
    query_terms = _terms(query)
    minus_terms = _terms(minus_context)
    candidate_terms = _terms(candidate_context)
    plus_terms = minus_terms | candidate_terms
    return (
        _coverage(query_terms, candidate_terms),
        _coverage(query_terms, plus_terms),
        len(candidate_terms - minus_terms) / max(len(candidate_terms), 1),
        _jaccard(candidate_terms, minus_terms),
        math.log1p(len(candidate_terms)) / 10,
    )


def _terms(text: str) -> set[str]:
    return {term.lower() for term in _TERM_PATTERN.findall(text)}


def _coverage(query: set[str], context: set[str]) -> float:
    return len(query & context) / max(len(query), 1)


def _jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / max(len(left | right), 1)


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    left_scale = math.sqrt(sum((x - left_mean) ** 2 for x in left))
    right_scale = math.sqrt(sum((y - right_mean) ** 2 for y in right))
    denominator = left_scale * right_scale
    return numerator / denominator if denominator else 0.0
