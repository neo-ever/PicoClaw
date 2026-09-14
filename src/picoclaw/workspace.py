"""Repository boundary used by local tools."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


class WorkspaceViolation(ValueError):
    """Raised when a tool attempts to leave the workspace."""


@dataclass(frozen=True, slots=True)
class Workspace:
    MAX_WRITE_BYTES = 1_000_000
    MAX_COMMAND_OUTPUT_CHARS = 30_000
    MAX_COMMAND_TIMEOUT = 120
    MAX_READ_CHARS = 30_000
    MAX_SEARCH_FILE_BYTES = 1_000_000
    EXCLUDED_DIRECTORIES: ClassVar[frozenset[str]] = frozenset(
        {
            ".git",
            ".mypy_cache",
            ".picoclaw",
            ".pytest_cache",
            ".ruff_cache",
            ".tox",
            ".venv",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
            "venv",
        }
    )
    SENSITIVE_NAMES: ClassVar[frozenset[str]] = frozenset(
        {".env", "credentials.json", "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"}
    )
    SENSITIVE_SUFFIXES: ClassVar[frozenset[str]] = frozenset({".key", ".p12", ".pfx", ".pem"})

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

    def read_text_range(
        self,
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
        max_chars: int = MAX_READ_CHARS,
    ) -> str:
        """Read a bounded line range without changing full reads used by verifiers."""

        if start_line < 1:
            raise ValueError("start_line must be at least 1")
        if end_line is not None and end_line < start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        if not 1 <= max_chars <= self.MAX_READ_CHARS:
            raise ValueError(f"max_chars must be between 1 and {self.MAX_READ_CHARS}")

        text = self.read_text(path)
        lines = text.splitlines(keepends=True)
        selected = "".join(lines[start_line - 1 : end_line])
        if len(selected) <= max_chars:
            return selected
        return selected[:max_chars] + f"\n[truncated at {max_chars} characters]"

    def list_files(self, pattern: str = "*", max_results: int = 200) -> str:
        """List bounded, non-sensitive repository files matching a glob pattern."""

        if not pattern or len(pattern) > 200:
            raise ValueError("pattern must contain between 1 and 200 characters")
        if not 1 <= max_results <= 500:
            raise ValueError("max_results must be between 1 and 500")

        matches: list[str] = []
        for path in self._iter_repository_files(self.root):
            relative = path.relative_to(self.root)
            if not relative.match(pattern):
                continue
            matches.append(relative.as_posix())
            if len(matches) >= max_results:
                break
        return "\n".join(matches) if matches else "No matching files found."

    def search_text(
        self,
        query: str,
        path: str = ".",
        file_pattern: str = "*",
        max_results: int = 50,
    ) -> str:
        """Search text safely without granting a general-purpose shell."""

        if not query or len(query) > 500:
            raise ValueError("query must contain between 1 and 500 characters")
        if not file_pattern or len(file_pattern) > 200:
            raise ValueError("file_pattern must contain between 1 and 200 characters")
        if not 1 <= max_results <= 100:
            raise ValueError("max_results must be between 1 and 100")

        scope = self.resolve(path)
        relative_scope = scope.relative_to(self.root)
        if any(self.is_excluded_directory(part) for part in relative_scope.parts):
            raise WorkspaceViolation(f"search scope is excluded: {path}")
        if not scope.exists():
            raise FileNotFoundError(f"search path does not exist: {path}")

        needle = query.casefold()
        matches: list[str] = []
        for candidate in self._iter_repository_files(scope):
            relative = candidate.relative_to(self.root)
            if not relative.match(file_pattern):
                continue
            try:
                if candidate.stat().st_size > self.MAX_SEARCH_FILE_BYTES:
                    continue
                contents = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(contents.splitlines(), start=1):
                if needle not in line.casefold():
                    continue
                excerpt = line.strip()[:500]
                matches.append(f"{relative.as_posix()}:{line_number}: {excerpt}")
                if len(matches) >= max_results:
                    return "\n".join(matches)
        return "\n".join(matches) if matches else "No matches found."

    @classmethod
    def is_excluded_directory(cls, name: str) -> bool:
        normalized = name.casefold()
        return normalized in cls.EXCLUDED_DIRECTORIES or normalized.startswith(
            (".venv-", ".venv_", "venv-", "venv_")
        )

    @classmethod
    def is_sensitive_file(cls, path: Path) -> bool:
        name = path.name.casefold()
        return (
            name in cls.SENSITIVE_NAMES
            or name.startswith(".env.")
            or path.suffix.casefold() in cls.SENSITIVE_SUFFIXES
        )

    def _iter_repository_files(self, scope: Path) -> Iterator[Path]:
        if scope.is_file():
            if not scope.is_symlink() and not self.is_sensitive_file(scope):
                yield scope
            return
        if not scope.is_dir():
            return

        for current_root, directory_names, file_names in os.walk(scope, followlinks=False):
            current = Path(current_root)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if not self.is_excluded_directory(name) and not (current / name).is_symlink()
            )
            for name in sorted(file_names):
                candidate = current / name
                if candidate.is_symlink() or self.is_sensitive_file(candidate):
                    continue
                yield candidate

    def sha256(self, path: str) -> str:
        resolved_path = self.resolve(path)
        if not resolved_path.is_file():
            raise FileNotFoundError(f"file does not exist: {path}")
        digest = hashlib.sha256()
        with resolved_path.open("rb") as file:
            for chunk in iter(lambda: file.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

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
