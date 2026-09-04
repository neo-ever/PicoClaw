"""Atomic checkpoints guarded by runtime identity and workspace hashes."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .task_state import TaskState
from .workspace import Workspace

if TYPE_CHECKING:
    from .agent import Agent


class CheckpointError(ValueError):
    """Base class for invalid or unavailable checkpoint state."""


class CheckpointStaleError(CheckpointError):
    """The runtime or workspace no longer matches the saved checkpoint."""


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    runtime_version: str
    provider: str
    model: str
    tool_config_sha256: str
    verifier_sha256: str
    implementation_sha256: str
    digest: str

    @classmethod
    def from_agent(
        cls,
        agent: Agent,
        verifier: object | None = None,
        runtime_version: str = "0.1.0",
    ) -> RuntimeIdentity:
        provider_type = type(agent.provider)
        provider = f"{provider_type.__module__}.{provider_type.__qualname__}"
        config = getattr(agent.provider, "config", None)
        model = str(getattr(config, "model", provider_type.__name__))
        tool_config = json.dumps(
            agent.tools.identity_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        tool_digest = hashlib.sha256(tool_config.encode("utf-8")).hexdigest()
        if verifier is not None and is_dataclass(verifier):
            verifier_payload = asdict(verifier)
        elif verifier is not None and hasattr(verifier, "__dict__"):
            verifier_payload = {
                "type": (f"{type(verifier).__module__}.{type(verifier).__qualname__}"),
                "state": vars(verifier),
            }
        else:
            verifier_payload = {
                "type": (
                    f"{type(verifier).__module__}.{type(verifier).__qualname__}"
                    if verifier is not None
                    else "none"
                )
            }
        verifier_json = json.dumps(
            verifier_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        verifier_digest = hashlib.sha256(verifier_json.encode("utf-8")).hexdigest()
        implementation_digest = cls._implementation_digest()
        payload = (
            f"{runtime_version}\0{provider}\0{model}\0{tool_digest}\0"
            f"{verifier_digest}\0{implementation_digest}"
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return cls(
            runtime_version,
            provider,
            model,
            tool_digest,
            verifier_digest,
            implementation_digest,
            digest,
        )

    @staticmethod
    def _implementation_digest() -> str:
        digest = hashlib.sha256()
        package_root = Path(__file__).parent
        for path in sorted(package_root.glob("*.py")):
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class WorkspaceManifest:
    files: dict[str, str]
    digest: str

    @classmethod
    def capture(cls, workspace: Workspace, max_files: int = 5_000) -> WorkspaceManifest:
        excluded = {".git", ".picoclaw", ".venv", ".pytest_cache", "__pycache__"}
        files: dict[str, str] = {}
        for path in sorted(workspace.root.rglob("*")):
            relative = path.relative_to(workspace.root)
            if any(part in excluded for part in relative.parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            files[relative.as_posix()] = cls._sha256(path)
            if len(files) > max_files:
                raise CheckpointError(f"workspace manifest exceeds {max_files} files")
        serialized = json.dumps(files, sort_keys=True, separators=(",", ":"))
        return cls(files, hashlib.sha256(serialized.encode("utf-8")).hexdigest())

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


class CheckpointStore:
    SCHEMA_VERSION = 1

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path_for(self, request: str) -> Path:
        goal_id = TaskState.create(request).goal_id
        return self.root / f"{goal_id}.json"

    def save(
        self,
        state: TaskState,
        identity: RuntimeIdentity,
        workspace: Workspace,
    ) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{state.goal_id}.json"
        manifest = WorkspaceManifest.capture(workspace)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "runtime_identity": asdict(identity),
            "workspace_manifest": asdict(manifest),
            "task_state": state.to_dict(),
        }
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
        return path

    def load(
        self,
        request: str,
        identity: RuntimeIdentity,
        workspace: Workspace,
    ) -> TaskState:
        path = self.path_for(request)
        if not path.is_file():
            raise CheckpointError(f"checkpoint does not exist: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != self.SCHEMA_VERSION:
                raise CheckpointStaleError("checkpoint schema version changed")
            saved_identity = payload["runtime_identity"]
            if saved_identity.get("digest") != identity.digest:
                raise CheckpointStaleError("runtime identity changed")
            saved_manifest = payload["workspace_manifest"]
            current_manifest = WorkspaceManifest.capture(workspace)
            if saved_manifest.get("digest") != current_manifest.digest:
                raise CheckpointStaleError("workspace files changed after checkpoint")
            state = TaskState.from_dict(payload["task_state"])
        except CheckpointError:
            raise
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise CheckpointError(f"invalid checkpoint: {path}") from exc
        if state.request != request.strip():
            raise CheckpointStaleError("checkpoint request changed")
        return state
