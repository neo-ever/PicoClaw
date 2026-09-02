"""Tool registration, validation, approval, and execution."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .models import ToolCall, ToolResult

if TYPE_CHECKING:
    from .workspace import Workspace

ToolHandler = Callable[..., str]
ApprovalPrompt = Callable[[str], str]


class RiskLevel(StrEnum):
    READ_ONLY = "read_only"
    WRITE = "write"
    EXECUTE = "execute"


class ApprovalMode(StrEnum):
    ASK = "ask"
    AUTO = "auto"
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    risk: RiskLevel = RiskLevel.READ_ONLY

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Approver:
    """Apply one explicit policy to risky tool calls."""

    def __init__(self, mode: ApprovalMode | str = ApprovalMode.NEVER, prompt: ApprovalPrompt = input):
        self.mode = ApprovalMode(mode)
        self.prompt = prompt

    def approve(self, tool: Tool, call: ToolCall) -> bool:
        if tool.risk is RiskLevel.READ_ONLY:
            return True
        if self.mode is ApprovalMode.AUTO:
            return True
        if self.mode is ApprovalMode.NEVER:
            return False

        arguments = self._safe_arguments(call.arguments)
        question = (
            f"Approve {tool.name} risk={tool.risk.value} "
            f"arguments={json.dumps(arguments, ensure_ascii=False)}? [y/N] "
        )
        try:
            answer = self.prompt(question)
        except EOFError:
            return False
        return answer.strip().lower() in {"y", "yes"}

    @staticmethod
    def _safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        safe = dict(arguments)
        if "content" in safe:
            content = str(safe["content"])
            safe["content"] = f"<redacted {len(content.encode('utf-8'))} bytes>"
        return safe


class ToolRegistry:
    def __init__(self, approver: Approver | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self.approver = approver or Approver(ApprovalMode.NEVER)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, call: ToolCall) -> ToolResult:
        started_at = time.monotonic()
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(
                call.id,
                call.name,
                f"unknown tool: {call.name}",
                is_error=True,
                metadata={"status": "rejected", "reason": "unknown_tool"},
            )

        base_metadata: dict[str, Any] = {
            "risk": tool.risk.value,
            "approval_required": tool.risk is not RiskLevel.READ_ONLY,
        }
        try:
            self._validate_arguments(tool, call.arguments)
            approved = self.approver.approve(tool, call)
            base_metadata["approved"] = approved
            if not approved:
                return ToolResult(
                    call.id,
                    call.name,
                    f"approval denied for {call.name}",
                    is_error=True,
                    metadata={**base_metadata, "status": "rejected", "reason": "approval_denied"},
                )
            content = str(tool.handler(**call.arguments))
        # Tool handlers are a trust boundary and may raise filesystem,
        # subprocess, or remote errors. Normalize them for the Agent Loop.
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call.id,
                call.name,
                f"{type(exc).__name__}: {exc}",
                is_error=True,
                metadata={
                    **base_metadata,
                    "status": "error",
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                },
            )

        return ToolResult(
            call.id,
            call.name,
            content,
            metadata={
                **base_metadata,
                "status": "success",
                "duration_ms": int((time.monotonic() - started_at) * 1000),
            },
        )

    @staticmethod
    def _validate_arguments(tool: Tool, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise TypeError("tool arguments must be an object")

        required = tool.parameters.get("required", [])
        missing = [name for name in required if name not in arguments]
        if missing:
            raise ValueError(f"missing required arguments: {', '.join(missing)}")

        properties = tool.parameters.get("properties", {})
        if tool.parameters.get("additionalProperties") is False:
            unexpected = sorted(set(arguments) - set(properties))
            if unexpected:
                raise ValueError(f"unexpected arguments: {', '.join(unexpected)}")

        for name, value in arguments.items():
            expected = properties.get(name, {}).get("type")
            if expected and not ToolRegistry._matches_json_type(value, expected):
                raise ValueError(f"argument '{name}' must have JSON type {expected}")

    @staticmethod
    def _matches_json_type(value: Any, expected: str) -> bool:
        checks = {
            "string": lambda item: isinstance(item, str),
            "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
            "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
            "boolean": lambda item: isinstance(item, bool),
            "object": lambda item: isinstance(item, dict),
            "array": lambda item: isinstance(item, list),
        }
        check = checks.get(expected)
        return True if check is None else check(value)


def _read_file_tool(workspace: Workspace) -> Tool:
    return Tool(
        name="read_file",
        description="Read one UTF-8 text file inside the current workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=workspace.read_text,
    )


def build_read_only_registry(workspace: Workspace) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_read_file_tool(workspace))
    return registry


def build_coding_registry(
    workspace: Workspace,
    approval_mode: ApprovalMode | str = ApprovalMode.ASK,
    approval_prompt: ApprovalPrompt = input,
) -> ToolRegistry:
    """Build the M3 local coding tool set under one approval policy."""

    registry = ToolRegistry(Approver(approval_mode, approval_prompt))
    registry.register(_read_file_tool(workspace))
    registry.register(
        Tool(
            name="write_file",
            description="Atomically write one UTF-8 file inside the current workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path"},
                    "content": {"type": "string", "description": "Complete file content"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=workspace.write_text,
            risk=RiskLevel.WRITE,
        )
    )
    registry.register(
        Tool(
            name="run_shell",
            description=(
                "Run one allowlisted development command without shell operators. "
                "Allowed families: pytest, ruff check, selected uv run commands, and read-only git."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "One allowlisted command"},
                    "timeout": {"type": "integer", "description": "Timeout from 1 to 120 seconds"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            handler=workspace.run_shell,
            risk=RiskLevel.EXECUTE,
        )
    )
    return registry
