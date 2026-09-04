"""Model-independent checks that inspect the real workspace."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .workspace import Workspace


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    summary: str
    checks: tuple[VerificationCheck, ...]


class Verifier(Protocol):
    def verify(self, workspace: Workspace, request: str) -> VerificationResult:
        """Check the workspace itself instead of trusting the model answer."""


@dataclass(frozen=True, slots=True)
class FileContentVerifier:
    path: str
    expected_content: str
    exact: bool = True

    def verify(self, workspace: Workspace, request: str) -> VerificationResult:
        del request
        try:
            actual = workspace.read_text(self.path)
        except (FileNotFoundError, UnicodeError, ValueError) as exc:
            check = VerificationCheck(
                name=f"file:{self.path}",
                passed=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
            return VerificationResult(False, f"required file check failed: {self.path}", (check,))

        passed = actual == self.expected_content if self.exact else self.expected_content in actual
        expectation = "exact content" if self.exact else "required substring"
        detail = (
            f"matched {expectation}"
            if passed
            else (f"expected {expectation} {self.expected_content!r}, got {actual[:300]!r}")
        )
        check = VerificationCheck(f"file:{self.path}", passed, detail)
        summary = f"file verification {'passed' if passed else 'failed'}: {self.path}"
        return VerificationResult(passed, summary, (check,))


@dataclass(frozen=True, slots=True)
class CommandVerifier:
    command: str
    expected_exit_code: int = 0
    output_contains: str | None = None
    timeout: int = 60

    def verify(self, workspace: Workspace, request: str) -> VerificationResult:
        del request
        try:
            output = workspace.run_shell(self.command, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            check = VerificationCheck(
                name=f"command:{self.command}",
                passed=False,
                detail=f"{type(exc).__name__}: {exc}",
            )
            return VerificationResult(False, "verification command could not run", (check,))

        first_line = output.splitlines()[0] if output else ""
        exit_passed = first_line == f"exit_code={self.expected_exit_code}"
        output_passed = self.output_contains is None or self.output_contains in output
        passed = exit_passed and output_passed
        detail = (
            f"expected exit_code={self.expected_exit_code}; "
            f"required_output={self.output_contains!r}; observed={output[:500]!r}"
        )
        check = VerificationCheck(f"command:{self.command}", passed, detail)
        return VerificationResult(
            passed,
            f"command verification {'passed' if passed else 'failed'}: {self.command}",
            (check,),
        )


@dataclass(frozen=True, slots=True)
class CompositeVerifier:
    verifiers: Sequence[Verifier]

    def verify(self, workspace: Workspace, request: str) -> VerificationResult:
        checks: list[VerificationCheck] = []
        summaries: list[str] = []
        for verifier in self.verifiers:
            result = verifier.verify(workspace, request)
            checks.extend(result.checks)
            summaries.append(result.summary)
        passed = bool(self.verifiers) and all(check.passed for check in checks)
        return VerificationResult(passed, "; ".join(summaries), tuple(checks))
