"""Budgeted, resumable Goal Loop with model-independent verification."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .agent import Agent, AgentBudgetExceeded, AgentRunBudget
from .checkpoint import CheckpointStore, RuntimeIdentity, WorkspaceManifest
from .models import TokenUsage
from .run_store import utc_now
from .task_state import AttemptRecord, GoalBudget, TaskState, TaskStatus, TokenPricing
from .verifier import VerificationResult, Verifier
from .workspace import Workspace


@dataclass(frozen=True, slots=True)
class GoalResult:
    state: TaskState
    verification: VerificationResult | None
    checkpoint_path: Path
    trace_path: Path
    report_path: Path

    @property
    def succeeded(self) -> bool:
        return self.state.status is TaskStatus.SUCCEEDED


class GoalRunner:
    def __init__(
        self,
        agent: Agent,
        workspace: Workspace,
        verifier: Verifier,
        checkpoint_store: CheckpointStore,
        budget: GoalBudget | None = None,
        identity: RuntimeIdentity | None = None,
        pricing: TokenPricing | None = None,
    ) -> None:
        self.agent = agent
        self.workspace = workspace
        self.verifier = verifier
        self.checkpoint_store = checkpoint_store
        self.budget = budget or GoalBudget()
        if self.budget.max_cost_usd is not None and pricing is None:
            raise ValueError("a TokenPricing configuration is required for a cost budget")
        self.pricing = pricing
        self.identity = identity or RuntimeIdentity.from_agent(agent, verifier)

    def run(self, request: str, *, resume: bool = False) -> GoalResult:
        state = (
            self.checkpoint_store.load(request, self.identity, self.workspace)
            if resume
            else TaskState.create(request)
        )
        paths = self._artifact_paths(state)
        if state.status is TaskStatus.SUCCEEDED:
            return GoalResult(
                state,
                None,
                self.checkpoint_store.path_for(request),
                paths[0],
                paths[1],
            )

        state.status = TaskStatus.RUNNING
        state.stopped_reason = None
        state.error_type = None
        self._emit(paths[0], "goal_started" if not resume else "goal_resumed", state)
        verification: VerificationResult | None = None

        while True:
            stop = self._stop_reason(state)
            if stop is not None:
                status = TaskStatus.STALLED if stop == "stagnation" else TaskStatus.BUDGET_EXHAUSTED
                return self._finish(state, status, stop, verification, paths)

            attempt_number = state.attempts + 1
            prompt = self._attempt_prompt(state)
            before = WorkspaceManifest.capture(self.workspace)
            attempt_started = time.monotonic()
            self._emit(
                paths[0],
                "attempt_started",
                state,
                {"attempt": attempt_number, "remaining": self._remaining(state)},
            )

            answer = ""
            outcome = "answered"
            budget_reason: str | None = None
            attempt_steps = 0
            attempt_tools = 0
            attempt_prompt_tokens = 0
            attempt_completion_tokens = 0
            attempt_tokens = 0
            try:
                result = self.agent.run(prompt, budget=self._agent_budget(state))
                answer = result.answer
                attempt_steps = result.steps
                attempt_tools = sum(event.kind == "tool_executed" for event in result.trace)
                attempt_prompt_tokens = result.usage.prompt_tokens
                attempt_completion_tokens = result.usage.completion_tokens
                attempt_tokens = result.usage.total_tokens
            except AgentBudgetExceeded as exc:
                outcome = "agent_budget_exceeded"
                budget_reason = exc.reason
                attempt_steps = exc.model_steps
                attempt_tools = exc.tool_calls
                attempt_prompt_tokens = exc.usage.prompt_tokens
                attempt_completion_tokens = exc.usage.completion_tokens
                attempt_tokens = exc.usage.total_tokens
                self._emit(
                    paths[0],
                    "agent_budget_exceeded",
                    state,
                    {"attempt": attempt_number, "reason": exc.reason},
                )
            except Exception as exc:  # noqa: BLE001
                duration = time.monotonic() - attempt_started
                state.attempts += 1
                state.elapsed_seconds += duration
                state.status = TaskStatus.FAILED
                state.stopped_reason = "agent_error"
                state.error_type = type(exc).__name__
                state.updated_at = utc_now()
                checkpoint_path = self.checkpoint_store.save(state, self.identity, self.workspace)
                self._emit(
                    paths[0],
                    "goal_failed",
                    state,
                    {"error_type": type(exc).__name__},
                )
                self._write_report(paths[1], state, None, checkpoint_path)
                return GoalResult(state, None, checkpoint_path, paths[0], paths[1])

            verification = self.verifier.verify(self.workspace, state.request)
            after = WorkspaceManifest.capture(self.workspace)
            duration = time.monotonic() - attempt_started
            workspace_changed = before.digest != after.digest
            feedback = self._verification_feedback(verification)
            attempt_usage = TokenUsage(
                prompt_tokens=attempt_prompt_tokens,
                completion_tokens=attempt_completion_tokens,
                total_tokens=attempt_tokens,
            )
            attempt_cost = self.pricing.estimate(attempt_usage) if self.pricing else 0.0

            state.attempts += 1
            state.model_steps += attempt_steps
            state.tool_calls += attempt_tools
            state.prompt_tokens += attempt_prompt_tokens
            state.completion_tokens += attempt_completion_tokens
            state.total_tokens += attempt_tokens
            state.estimated_cost_usd += attempt_cost
            state.elapsed_seconds += duration
            state.last_answer = answer
            state.last_verification_summary = feedback
            state.stagnant_attempts = (
                0 if workspace_changed or verification.passed else (state.stagnant_attempts + 1)
            )
            state.history.append(
                AttemptRecord(
                    number=attempt_number,
                    prompt=prompt,
                    outcome=outcome,
                    answer=answer,
                    model_steps=attempt_steps,
                    tool_calls=attempt_tools,
                    prompt_tokens=attempt_prompt_tokens,
                    completion_tokens=attempt_completion_tokens,
                    total_tokens=attempt_tokens,
                    estimated_cost_usd=attempt_cost,
                    duration_seconds=duration,
                    workspace_changed=workspace_changed,
                    verification_passed=verification.passed,
                    verification_summary=feedback,
                    budget_reason=budget_reason,
                )
            )
            state.updated_at = utc_now()
            checkpoint_path = self.checkpoint_store.save(state, self.identity, self.workspace)
            self._emit(
                paths[0],
                "verification_finished",
                state,
                {
                    "attempt": attempt_number,
                    "passed": verification.passed,
                    "summary": verification.summary,
                    "workspace_changed": workspace_changed,
                },
            )
            self._emit(
                paths[0],
                "checkpoint_saved",
                state,
                {"path": str(checkpoint_path)},
            )

            if verification.passed:
                return self._finish(
                    state,
                    TaskStatus.SUCCEEDED,
                    "verified",
                    verification,
                    paths,
                )

    def _agent_budget(self, state: TaskState) -> AgentRunBudget:
        remaining = self._remaining(state)
        return AgentRunBudget(
            max_model_steps=max(1, min(self.agent.max_steps, remaining["model_steps"])),
            max_tool_calls=remaining["tool_calls"],
            max_total_tokens=remaining["total_tokens"],
            max_seconds=remaining["seconds"],
        )

    def _remaining(self, state: TaskState) -> dict[str, Any]:
        return {
            "attempts": self.budget.max_attempts - state.attempts,
            "model_steps": self.budget.max_model_steps - state.model_steps,
            "tool_calls": self.budget.max_tool_calls - state.tool_calls,
            "total_tokens": self.budget.max_total_tokens - state.total_tokens,
            "cost_usd": (
                None
                if self.budget.max_cost_usd is None
                else self.budget.max_cost_usd - state.estimated_cost_usd
            ),
            "seconds": max(0.001, self.budget.max_seconds - state.elapsed_seconds),
            "stagnant_attempts": (self.budget.max_stagnant_attempts - state.stagnant_attempts),
        }

    def _stop_reason(self, state: TaskState) -> str | None:
        if state.stagnant_attempts >= self.budget.max_stagnant_attempts:
            return "stagnation"
        limits = (
            (state.attempts, self.budget.max_attempts, "attempts"),
            (state.model_steps, self.budget.max_model_steps, "model_steps"),
            (state.tool_calls, self.budget.max_tool_calls, "tool_calls"),
            (state.total_tokens, self.budget.max_total_tokens, "total_tokens"),
            (state.elapsed_seconds, self.budget.max_seconds, "time"),
        )
        exhausted = next((name for value, limit, name in limits if value >= limit), None)
        if exhausted is not None:
            return exhausted
        if (
            self.budget.max_cost_usd is not None
            and state.estimated_cost_usd >= self.budget.max_cost_usd
        ):
            return "cost"
        return None

    @staticmethod
    def _attempt_prompt(state: TaskState) -> str:
        if state.attempts == 0:
            return state.request
        return (
            f"Original goal:\n{state.request}\n\n"
            f"Previous answer:\n{state.last_answer or '<no final answer>'}\n\n"
            f"Independent verifier feedback:\n{state.last_verification_summary}\n\n"
            "Continue from the current workspace. Fix the verified failure; do not merely claim success."
        )

    @staticmethod
    def _verification_feedback(result: VerificationResult) -> str:
        details = "\n".join(
            f"- {check.name}: {'PASS' if check.passed else 'FAIL'} — {check.detail}"
            for check in result.checks
        )
        return f"{result.summary}\n{details}".strip()

    def _finish(
        self,
        state: TaskState,
        status: TaskStatus,
        reason: str,
        verification: VerificationResult | None,
        paths: tuple[Path, Path],
    ) -> GoalResult:
        state.status = status
        state.stopped_reason = reason
        state.updated_at = utc_now()
        checkpoint_path = self.checkpoint_store.save(state, self.identity, self.workspace)
        self._emit(
            paths[0],
            "goal_finished",
            state,
            {"status": status.value, "reason": reason},
        )
        self._write_report(paths[1], state, verification, checkpoint_path)
        return GoalResult(state, verification, checkpoint_path, paths[0], paths[1])

    def _artifact_paths(self, state: TaskState) -> tuple[Path, Path]:
        directory = self.checkpoint_store.root.parent / "goals" / state.goal_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory / "trace.jsonl", directory / "report.json"

    @staticmethod
    def _emit(
        trace_path: Path,
        kind: str,
        state: TaskState,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        event = {
            "created_at": utc_now(),
            "kind": kind,
            "goal_id": state.goal_id,
            "metadata": metadata or {},
        }
        with trace_path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def _write_report(
        report_path: Path,
        state: TaskState,
        verification: VerificationResult | None,
        checkpoint_path: Path,
    ) -> None:
        payload = {
            "task_state": state.to_dict(),
            "verification": asdict(verification) if verification is not None else None,
            "checkpoint_path": str(checkpoint_path),
            "finished_at": utc_now(),
        }
        temporary = report_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, report_path)
