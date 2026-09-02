"""Repository boundary used by local tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspaceViolation(ValueError):
    """Raised when a tool attempts to leave the workspace."""


@dataclass(frozen=True, slots=True)
class Workspace:
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
