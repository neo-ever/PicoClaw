"""Local append-only trace and final run report persistence."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class RunHandle:
    run_id: str
    directory: Path
    trace_path: Path
    report_path: Path


class RunStore:
    """Store one directory per run under `.picoclaw/runs`."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def start(self) -> RunHandle:
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        run_id = f"run-{timestamp}-{uuid.uuid4().hex[:8]}"
        directory = self.root / run_id
        directory.mkdir(parents=True, exist_ok=False)
        return RunHandle(
            run_id=run_id,
            directory=directory,
            trace_path=directory / "trace.jsonl",
            report_path=directory / "report.json",
        )

    @staticmethod
    def append_trace(handle: RunHandle, event: dict[str, Any]) -> None:
        with handle.trace_path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def write_report(handle: RunHandle, report: dict[str, Any]) -> None:
        temporary_path = handle.report_path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, handle.report_path)
