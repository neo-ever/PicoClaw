"""Serializable state and budgets for long-running goals."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from .models import TokenUsage
from .run_store import utc_now


class TaskStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    BUDGET_EXHAUSTED = "budget_exhausted"
    STALLED = "stalled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class GoalBudget:
    max_attempts: int = 3
    max_model_steps: int = 18
    max_tool_calls: int = 24
    max_total_tokens: int = 50_000
    max_cost_usd: float | None = None
    max_seconds: float = 300.0
    max_stagnant_attempts: int = 2

    def __post_init__(self) -> None:
        if (
            min(
                self.max_attempts,
                self.max_model_steps,
                self.max_tool_calls,
                self.max_total_tokens,
                self.max_stagnant_attempts,
            )
            < 1
        ):
            raise ValueError("goal budgets must be positive")
        if self.max_seconds <= 0:
            raise ValueError("goal max_seconds must be positive")
        if self.max_cost_usd is not None and self.max_cost_usd <= 0:
            raise ValueError("goal max_cost_usd must be positive when configured")


@dataclass(frozen=True, slots=True)
class TokenPricing:
    input_per_million_usd: float
    output_per_million_usd: float

    def __post_init__(self) -> None:
        if min(self.input_per_million_usd, self.output_per_million_usd) < 0:
            raise ValueError("token prices cannot be negative")

    def estimate(self, usage: TokenUsage) -> float:
        return (
            usage.prompt_tokens * self.input_per_million_usd
            + usage.completion_tokens * self.output_per_million_usd
        ) / 1_000_000


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    number: int
    prompt: str
    outcome: str
    answer: str
    model_steps: int
    tool_calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    duration_seconds: float
    workspace_changed: bool
    verification_passed: bool
    verification_summary: str
    budget_reason: str | None = None


@dataclass(slots=True)
class TaskState:
    goal_id: str
    request: str
    status: TaskStatus = TaskStatus.RUNNING
    attempts: int = 0
    model_steps: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    elapsed_seconds: float = 0.0
    stagnant_attempts: int = 0
    last_answer: str = ""
    last_verification_summary: str = ""
    stopped_reason: str | None = None
    error_type: str | None = None
    history: list[AttemptRecord] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(cls, request: str) -> TaskState:
        normalized = request.strip()
        if not normalized:
            raise ValueError("goal request must not be empty")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
        return cls(goal_id=f"goal-{digest}", request=normalized)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> TaskState:
        return cls(
            goal_id=str(data["goal_id"]),
            request=str(data["request"]),
            status=TaskStatus(data.get("status", TaskStatus.RUNNING.value)),
            attempts=int(data.get("attempts", 0)),
            model_steps=int(data.get("model_steps", 0)),
            tool_calls=int(data.get("tool_calls", 0)),
            prompt_tokens=int(data.get("prompt_tokens", 0)),
            completion_tokens=int(data.get("completion_tokens", 0)),
            total_tokens=int(data.get("total_tokens", 0)),
            estimated_cost_usd=float(data.get("estimated_cost_usd", 0.0)),
            elapsed_seconds=float(data.get("elapsed_seconds", 0.0)),
            stagnant_attempts=int(data.get("stagnant_attempts", 0)),
            last_answer=str(data.get("last_answer", "")),
            last_verification_summary=str(data.get("last_verification_summary", "")),
            stopped_reason=data.get("stopped_reason"),
            error_type=data.get("error_type"),
            history=[AttemptRecord(**item) for item in data.get("history", [])],
            created_at=str(data.get("created_at", utc_now())),
            updated_at=str(data.get("updated_at", utc_now())),
        )
