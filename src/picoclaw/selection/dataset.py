"""Code-domain marginal-outcome dataset schema shared by training and inference."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .models import ContextChunk

SCHEMA_VERSION = "picoclaw-delta-v1"


class OutcomeScorer(Protocol):
    def score(self, query: str, context: str) -> float:
        """Return a fixed Reader/Verifier outcome for this evidence context."""


@dataclass(frozen=True, slots=True)
class DeltaExample:
    example_id: str
    task_id: str
    query: str
    minus_context: str
    candidate_context: str
    plus_context: str
    score_minus: float
    score_plus: float
    delta: float
    label: int
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not all((self.example_id, self.task_id, self.query.strip(), self.source)):
            raise ValueError("delta example identifiers, query, and source must not be empty")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported delta schema: {self.schema_version}")
        if self.label not in {0, 1}:
            raise ValueError("delta label must be 0 or 1")
        if not all(
            math.isfinite(value) for value in (self.score_minus, self.score_plus, self.delta)
        ):
            raise ValueError("delta scores must be finite")
        if not math.isclose(
            self.delta,
            self.score_plus - self.score_minus,
            rel_tol=1e-7,
            abs_tol=1e-7,
        ):
            raise ValueError("delta must equal score_plus - score_minus")
        if self.label != int(self.delta > 0):
            raise ValueError("delta label must represent a positive marginal outcome")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DeltaExample:
        return cls(
            example_id=str(payload["example_id"]),
            task_id=str(payload["task_id"]),
            query=str(payload["query"]),
            minus_context=str(payload.get("minus_context", "")),
            candidate_context=str(payload.get("candidate_context", "")),
            plus_context=str(payload.get("plus_context", "")),
            score_minus=float(payload["score_minus"]),
            score_plus=float(payload["score_plus"]),
            delta=float(payload["delta"]),
            label=int(payload["label"]),
            source=str(payload["source"]),
            metadata=dict(payload.get("metadata", {})),
            schema_version=str(payload.get("schema_version", "")),
        )


class DeltaDatasetBuilder:
    """Label candidate additions using one fixed outcome-scoring contract."""

    def __init__(self, scorer: OutcomeScorer, source: str) -> None:
        if not source:
            raise ValueError("dataset source must not be empty")
        self.scorer = scorer
        self.source = source

    def build_task(
        self,
        task_id: str,
        query: str,
        candidates: Sequence[ContextChunk],
        base_chunks: Sequence[ContextChunk] = (),
    ) -> tuple[DeltaExample, ...]:
        if not task_id or not query.strip():
            raise ValueError("task_id and query must not be empty")
        minus_context = render_chunks(base_chunks)
        score_minus = float(self.scorer.score(query, minus_context))
        if not math.isfinite(score_minus):
            raise ValueError("outcome scorer returned a non-finite minus score")
        base_ids = {chunk.id for chunk in base_chunks}
        examples: list[DeltaExample] = []
        for candidate in candidates:
            if candidate.id in base_ids:
                continue
            candidate_context = render_chunks([candidate])
            plus_context = render_chunks([*base_chunks, candidate])
            score_plus = float(self.scorer.score(query, plus_context))
            if not math.isfinite(score_plus):
                raise ValueError("outcome scorer returned a non-finite plus score")
            delta = score_plus - score_minus
            identity = f"{SCHEMA_VERSION}\0{task_id}\0{candidate.id}\0{minus_context}"
            examples.append(
                DeltaExample(
                    example_id=hashlib.sha256(identity.encode()).hexdigest()[:20],
                    task_id=task_id,
                    query=query,
                    minus_context=minus_context,
                    candidate_context=candidate_context,
                    plus_context=plus_context,
                    score_minus=score_minus,
                    score_plus=score_plus,
                    delta=delta,
                    label=int(delta > 0),
                    source=self.source,
                    metadata={
                        "candidate_id": candidate.id,
                        "candidate_path": candidate.path,
                        "candidate_start_line": candidate.start_line,
                        "candidate_end_line": candidate.end_line,
                        "candidate_source_sha256": candidate.source_sha256,
                    },
                )
            )
        return tuple(examples)


def render_chunks(chunks: Sequence[ContextChunk]) -> str:
    return "\n\n".join(
        f"[file={chunk.path} lines={chunk.start_line}-{chunk.end_line}]\n{chunk.content}"
        for chunk in chunks
    )


def format_delta_prompt(query: str, minus_context: str, candidate_context: str) -> str:
    """The exact prompt contract shared by Qwen training and online serving."""

    return (
        "Estimate the marginal verifier outcome of adding one repository chunk.\n"
        "Return one real-valued delta; positive means the candidate helps.\n\n"
        f"TASK:\n{query}\n\n"
        f"CURRENT_EVIDENCE:\n{minus_context or '[empty]'}\n\n"
        f"CANDIDATE:\n{candidate_context or '[empty]'}\n"
    )


def write_delta_jsonl(path: Path, examples: Sequence[DeltaExample]) -> Path:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        for example in examples:
            file.write(json.dumps(example.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(temporary, path)
    return path


def read_delta_jsonl(path: Path) -> tuple[DeltaExample, ...]:
    examples: list[DeltaExample] = []
    with path.resolve().open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise TypeError("record must be an object")
                examples.append(DeltaExample.from_dict(payload))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"invalid delta JSONL record at line {line_number}") from exc
    return tuple(examples)


def split_delta_examples(
    examples: Sequence[DeltaExample],
    validation_ratio: float = 0.2,
    seed: int = 42,
) -> tuple[tuple[DeltaExample, ...], tuple[DeltaExample, ...]]:
    if not 0 < validation_ratio < 1:
        raise ValueError("validation_ratio must be between 0 and 1")
    task_ids = sorted({example.task_id for example in examples})
    if len(task_ids) < 2:
        raise ValueError("grouped split requires at least two task ids")
    random.Random(seed).shuffle(task_ids)
    validation_count = min(
        len(task_ids) - 1,
        max(1, round(len(task_ids) * validation_ratio)),
    )
    validation_ids = set(task_ids[:validation_count])
    train = tuple(example for example in examples if example.task_id not in validation_ids)
    validation = tuple(example for example in examples if example.task_id in validation_ids)
    return train, validation
