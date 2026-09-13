"""Safe, deterministic repository text chunking."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol

from ..workspace import Workspace
from .models import ContextChunk


class ChunkTokenCounter(Protocol):
    def count_text(self, text: str) -> int: ...

    def truncate_text(self, text: str, limit: int) -> str: ...


@dataclass(frozen=True, slots=True)
class ChunkingConfig:
    max_chunk_tokens: int = 320
    overlap_lines: int = 4
    max_file_bytes: int = 256_000
    max_files: int = 2_000
    max_chunks: int = 5_000

    def __post_init__(self) -> None:
        if (
            min(
                self.max_chunk_tokens,
                self.max_file_bytes,
                self.max_files,
                self.max_chunks,
            )
            < 1
        ):
            raise ValueError("chunking limits must be positive")
        if self.overlap_lines < 0:
            raise ValueError("overlap_lines cannot be negative")


class RepositoryChunker:
    """Read only allowlisted text files under a Workspace boundary."""

    ALLOWED_SUFFIXES: ClassVar[set[str]] = {
        ".c",
        ".cc",
        ".cfg",
        ".cpp",
        ".cs",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".md",
        ".php",
        ".ps1",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".svelte",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
    }
    ALLOWED_NAMES: ClassVar[set[str]] = {"dockerfile", "makefile", "procfile"}
    EXCLUDED_DIRECTORIES: ClassVar[set[str]] = {
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
    SENSITIVE_NAMES: ClassVar[set[str]] = {
        ".env",
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
    }
    SENSITIVE_SUFFIXES: ClassVar[set[str]] = {".key", ".p12", ".pfx", ".pem"}

    def __init__(
        self,
        workspace: Workspace,
        token_counter: ChunkTokenCounter,
        config: ChunkingConfig | None = None,
    ) -> None:
        self.workspace = workspace
        self.token_counter = token_counter
        self.config = config or ChunkingConfig()

    def chunk_repository(self) -> tuple[ContextChunk, ...]:
        chunks: list[ContextChunk] = []
        files_seen = 0
        for path in sorted(self.workspace.root.rglob("*"), key=lambda item: item.as_posix()):
            if len(chunks) >= self.config.max_chunks or files_seen >= self.config.max_files:
                break
            if not self._is_candidate(path):
                continue
            files_seen += 1
            relative_path = path.relative_to(self.workspace.root).as_posix()
            chunks.extend(self.chunk_file(relative_path))
            if len(chunks) > self.config.max_chunks:
                del chunks[self.config.max_chunks :]
        return tuple(chunks)

    def chunk_file(self, relative_path: str) -> tuple[ContextChunk, ...]:
        path = self.workspace.resolve(relative_path)
        if not self._is_candidate(path):
            return ()
        raw = path.read_bytes()
        if b"\x00" in raw:
            return ()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ()
        lines = text.splitlines()
        if not lines:
            return ()

        normalized_path = path.relative_to(self.workspace.root).as_posix()
        source_sha256 = hashlib.sha256(raw).hexdigest()
        chunks: list[ContextChunk] = []
        start = 0
        while start < len(lines):
            end = start
            selected_lines: list[str] = []
            while end < len(lines):
                candidate = "\n".join([*selected_lines, lines[end]])
                candidate_tokens = self.token_counter.count_text(candidate)
                if selected_lines and candidate_tokens > self.config.max_chunk_tokens:
                    break
                if not selected_lines and candidate_tokens > self.config.max_chunk_tokens:
                    selected_lines.append(
                        self.token_counter.truncate_text(
                            lines[end],
                            self.config.max_chunk_tokens,
                        )
                    )
                    end += 1
                    break
                selected_lines.append(lines[end])
                end += 1

            content = "\n".join(selected_lines).strip()
            if content:
                start_line = start + 1
                end_line = end
                token_count = self.token_counter.count_text(content)
                identity = f"{normalized_path}:{start_line}:{end_line}:{source_sha256}"
                chunks.append(
                    ContextChunk(
                        id=hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
                        path=normalized_path,
                        start_line=start_line,
                        end_line=end_line,
                        content=content,
                        source_sha256=source_sha256,
                        token_count=token_count,
                    )
                )

            if end >= len(lines):
                break
            start = max(start + 1, end - self.config.overlap_lines)
        return tuple(chunks)

    def _is_candidate(self, path: Path) -> bool:
        if not path.is_file() or path.is_symlink():
            return False
        try:
            relative = path.resolve().relative_to(self.workspace.root)
        except ValueError:
            return False
        if any(part.lower() in self.EXCLUDED_DIRECTORIES for part in relative.parts[:-1]):
            return False
        name = path.name.lower()
        if name in self.SENSITIVE_NAMES or name.startswith(".env."):
            return False
        if path.suffix.lower() in self.SENSITIVE_SUFFIXES:
            return False
        if path.suffix.lower() not in self.ALLOWED_SUFFIXES and name not in self.ALLOWED_NAMES:
            return False
        try:
            return path.stat().st_size <= self.config.max_file_bytes
        except OSError:
            return False
