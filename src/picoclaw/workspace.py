"""Repository boundary used by local tools."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


class WorkspaceViolation(ValueError):
    """Raised when a tool attempts to leave the workspace."""


@dataclass(frozen=True, slots=True)
class Workspace:
    MAX_WRITE_BYTES = 1_000_000
    MAX_COMMAND_OUTPUT_CHARS = 30_000
    MAX_COMMAND_TIMEOUT = 120

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    def resolve(self, relative_path: str) -> Path:
        """Resolve a user/model path and guarantee it stays under the repository root."""

        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceViolation(f"path escapes workspace: {relative_path}") from exc
        return candidate

    def read_text(self, path: str) -> str:
        resolved_path = self.resolve(path)
        if not resolved_path.is_file():
            raise FileNotFoundError(f"file does not exist: {path}")
        return resolved_path.read_text(encoding="utf-8")

    def write_text(self, path: str, content: str) -> str:
        """Atomically write a small UTF-8 file inside the workspace."""

        resolved_path = self.resolve(path)
        encoded = content.encode("utf-8")
        if len(encoded) > self.MAX_WRITE_BYTES:
            raise ValueError(f"content exceeds {self.MAX_WRITE_BYTES} byte limit")
        resolved_path.parent.mkdir(parents=True, exist_ok=True)

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=resolved_path.parent,
                prefix=f".{resolved_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_file.write(encoded)
                temporary_path = Path(temporary_file.name)
            os.replace(temporary_path, resolved_path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

        relative = resolved_path.relative_to(self.root)
        return f"wrote {len(encoded)} bytes to {relative.as_posix()}"

    def run_shell(self, command: str, timeout: int = 20) -> str:
        """Run a narrow allowlist of development commands without a shell interpreter."""

        command = command.strip()
        if not command:
            raise ValueError("command must not be empty")
        if not 1 <= timeout <= self.MAX_COMMAND_TIMEOUT:
            raise ValueError(f"timeout must be between 1 and {self.MAX_COMMAND_TIMEOUT} seconds")
        if any(character in command for character in ("\n", "\r", ";", "&", "|", ">", "<")):
            raise ValueError("shell operators and multiple commands are not allowed")

        arguments = shlex.split(command, posix=os.name != "nt")
        if not arguments:
            raise ValueError("command must not be empty")
        if "/" in arguments[0] or "\\" in arguments[0]:
            raise ValueError("command executable must be a bare allowlisted name")
        if any(".." in argument.replace("\\", "/").split("/") for argument in arguments[1:]):
            raise ValueError("parent path traversal is not allowed in command arguments")

        self._validate_command(arguments)
        arguments = self._bind_python_environment(arguments)
        try:
            completed = subprocess.run(
                arguments,
                cwd=self.root,
                env=self._safe_subprocess_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"command exceeded {timeout} second timeout") from exc

        stdout = (completed.stdout or "")[: self.MAX_COMMAND_OUTPUT_CHARS]
        stderr = (completed.stderr or "")[: self.MAX_COMMAND_OUTPUT_CHARS]
        return f"exit_code={completed.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}".rstrip()

    @staticmethod
    def _validate_command(arguments: list[str]) -> None:
        program = arguments[0].lower()
        suffix = [item.lower() for item in arguments[1:]]

        if program in {"pytest", "pytest.exe"}:
            return
        if program in {"ruff", "ruff.exe"} and suffix[:1] == ["check"]:
            return
        if program in {"python", "python.exe"} and suffix[:2] == ["-m", "pytest"]:
            return
        if program in {"uv", "uv.exe"}:
            if suffix[:2] in (["run", "pytest"], ["run", "ruff"]):
                return
            if suffix[:4] == ["run", "python", "-m", "pytest"]:
                return
        if program in {"git", "git.exe"} and suffix[:1] in (
            ["status"],
            ["diff"],
            ["log"],
            ["show"],
            ["rev-parse"],
        ):
            return
        raise ValueError("command is outside the development-command allowlist")

    @staticmethod
    def _bind_python_environment(arguments: list[str]) -> list[str]:
        """Use the running virtual environment instead of an ambiguous PATH Python."""

        program = arguments[0].lower()
        if program in {"python", "python.exe"}:
            return [sys.executable, *arguments[1:]]
        if program in {"pytest", "pytest.exe"}:
            return [sys.executable, "-m", "pytest", *arguments[1:]]
        return arguments

    @staticmethod
    def _safe_subprocess_env() -> dict[str, str]:
        allowed_names = {
            "APPDATA",
            "HOME",
            "LOCALAPPDATA",
            "PATH",
            "PATHEXT",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "USERPROFILE",
            "VIRTUAL_ENV",
            "WINDIR",
        }
        return {name: value for name, value in os.environ.items() if name.upper() in allowed_names}
