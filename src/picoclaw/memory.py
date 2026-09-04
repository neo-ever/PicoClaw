"""Three-layer memory with file-hash freshness checks."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from .models import ToolCall, ToolResult
from .run_store import utc_now
from .workspace import Workspace


class MemoryLayer(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    DURABLE = "durable"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    layer: MemoryLayer
    content: str
    created_at: str
    source_path: str | None = None
    source_sha256: str | None = None
    dependencies: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        layer: MemoryLayer,
        content: str,
        *,
        source_path: str | None = None,
        source_sha256: str | None = None,
        dependencies: dict[str, str] | None = None,
    ) -> MemoryRecord:
        return cls(
            id=f"memory-{uuid.uuid4().hex[:10]}",
            layer=layer,
            content=content,
            created_at=utc_now(),
            source_path=source_path,
            source_sha256=source_sha256,
            dependencies=dict(dependencies or {}),
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["layer"] = self.layer.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> MemoryRecord:
        return cls(
            id=str(data["id"]),
            layer=MemoryLayer(data["layer"]),
            content=str(data["content"]),
            created_at=str(data["created_at"]),
            source_path=data.get("source_path"),
            source_sha256=data.get("source_sha256"),
            dependencies=dict(data.get("dependencies", {})),
        )


class MemoryManager:
    """Own current-task working memory and persisted episodic/durable memory."""

    MAX_FILE_EXCERPT_CHARS = 2000
    MAX_EPISODIC_RECORDS = 100
    MAX_DURABLE_RECORDS = 100

    def __init__(self, workspace: Workspace, path: Path | None = None) -> None:
        self.workspace = workspace
        self.path = (path or workspace.root / ".picoclaw" / "memory.json").resolve()
        self.working: list[MemoryRecord] = []
        self.episodic: list[MemoryRecord] = []
        self.durable: list[MemoryRecord] = []
        self.last_invalidated: list[MemoryRecord] = []
        self.last_retrieved: list[MemoryRecord] = []
        self._load()

    def start_task(self) -> None:
        self.working.clear()
        self.last_invalidated.clear()
        self.last_retrieved.clear()

    def observe_tool(self, call: ToolCall, result: ToolResult) -> None:
        if result.is_error:
            return
        path = str(call.arguments.get("path", "")).strip()
        if call.name == "write_file" and path:
            self.invalidate_path(path)
            resolved = self.workspace.resolve(path)
            relative = resolved.relative_to(self.workspace.root).as_posix()
            digest = self.workspace.sha256(relative)
            written_content = str(call.arguments.get("content", ""))
            self.working.append(
                MemoryRecord.create(
                    MemoryLayer.WORKING,
                    f"Current content written to {relative}:\n"
                    f"{written_content[: self.MAX_FILE_EXCERPT_CHARS]}",
                    source_path=relative,
                    source_sha256=digest,
                )
            )
            return
        if call.name != "read_file" or not path:
            return

        resolved = self.workspace.resolve(path)
        relative = resolved.relative_to(self.workspace.root).as_posix()
        digest = self.workspace.sha256(relative)
        excerpt = result.content[: self.MAX_FILE_EXCERPT_CHARS]
        content = f"File excerpt from {relative}:\n{excerpt}"
        self.working = [record for record in self.working if record.source_path != relative]
        self.working.append(
            MemoryRecord.create(
                MemoryLayer.WORKING,
                content,
                source_path=relative,
                source_sha256=digest,
            )
        )

    def relevant_entries(self, request: str, limit: int = 3) -> list[str]:
        if limit < 1:
            return []
        self._prune_stale_file_records()
        records = [*self.working, *self.episodic, *self.durable]
        request_terms = self._terms(request)
        ranked: list[tuple[float, int, MemoryRecord]] = []
        layer_weight = {
            MemoryLayer.WORKING: 3.0,
            MemoryLayer.EPISODIC: 1.0,
            MemoryLayer.DURABLE: 2.0,
        }
        for index, record in enumerate(records):
            overlap = len(request_terms & self._terms(record.content))
            path_bonus = (
                2 if record.source_path and record.source_path.lower() in request.lower() else 0
            )
            score = overlap * 10 + path_bonus + layer_weight[record.layer]
            if overlap or path_bonus or record.layer is MemoryLayer.WORKING:
                ranked.append((score, index, record))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        self.last_retrieved = [record for _, _, record in ranked[:limit]]
        return [self._render(record) for record in self.last_retrieved]

    def finish_task(self, request: str, answer: str) -> None:
        for record in self.working:
            if record.source_path:
                self.episodic = [
                    item for item in self.episodic if item.source_path != record.source_path
                ]
            self.episodic.append(replace(record, layer=MemoryLayer.EPISODIC))
        dependencies = {
            path: digest
            for record in [*self.last_retrieved, *self.working]
            for path, digest in self._record_dependencies(record).items()
        }
        self.episodic.append(
            MemoryRecord.create(
                MemoryLayer.EPISODIC,
                f"Task: {request}\nOutcome: {answer[:1000]}",
                dependencies=dependencies,
            )
        )
        self.episodic = self.episodic[-self.MAX_EPISODIC_RECORDS :]
        self._persist()

    def add_durable(self, content: str, source_path: str | None = None) -> MemoryRecord:
        content = content.strip()
        if not content:
            raise ValueError("durable memory content must not be empty")
        relative = None
        digest = None
        if source_path:
            resolved = self.workspace.resolve(source_path)
            relative = resolved.relative_to(self.workspace.root).as_posix()
            digest = self.workspace.sha256(relative)
        record = MemoryRecord.create(
            MemoryLayer.DURABLE,
            content,
            source_path=relative,
            source_sha256=digest,
        )
        self.durable.append(record)
        self.durable = self.durable[-self.MAX_DURABLE_RECORDS :]
        self._persist()
        return record

    def invalidate_path(self, path: str) -> int:
        resolved = self.workspace.resolve(path)
        relative = resolved.relative_to(self.workspace.root).as_posix()
        before = [*self.working, *self.episodic, *self.durable]
        removed = [
            record
            for record in before
            if record.source_path == relative or relative in record.dependencies
        ]
        removed_ids = {record.id for record in removed}
        self.working = [record for record in self.working if record.id not in removed_ids]
        self.episodic = [record for record in self.episodic if record.id not in removed_ids]
        self.durable = [record for record in self.durable if record.id not in removed_ids]
        self.last_invalidated.extend(removed)
        if removed:
            self._persist()
        return len(removed)

    def _prune_stale_file_records(self) -> None:
        stale: list[MemoryRecord] = []
        for record in [*self.working, *self.episodic, *self.durable]:
            for path, expected_digest in self._record_dependencies(record).items():
                try:
                    current = self.workspace.sha256(path)
                except FileNotFoundError:
                    stale.append(record)
                    break
                if current != expected_digest:
                    stale.append(record)
                    break
        if not stale:
            return
        stale_ids = {record.id for record in stale}
        self.working = [record for record in self.working if record.id not in stale_ids]
        self.episodic = [record for record in self.episodic if record.id not in stale_ids]
        self.durable = [record for record in self.durable if record.id not in stale_ids]
        self.last_invalidated.extend(stale)
        self._persist()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.episodic = [MemoryRecord.from_dict(item) for item in data.get("episodic", [])]
            self.durable = [MemoryRecord.from_dict(item) for item in data.get("durable", [])]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid memory file: {self.path}") from exc

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        data = {
            "version": 1,
            "episodic": [record.to_dict() for record in self.episodic],
            "durable": [record.to_dict() for record in self.durable],
        }
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    @staticmethod
    def _terms(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9_./-]+|[\u4e00-\u9fff]", text.lower()))

    @staticmethod
    def _render(record: MemoryRecord) -> str:
        freshness = f" sha256={record.source_sha256[:12]}" if record.source_sha256 else ""
        return f"[{record.layer.value}{freshness}] {record.content}"

    @staticmethod
    def _record_dependencies(record: MemoryRecord) -> dict[str, str]:
        dependencies = dict(record.dependencies)
        if record.source_path and record.source_sha256:
            dependencies[record.source_path] = record.source_sha256
        return dependencies
