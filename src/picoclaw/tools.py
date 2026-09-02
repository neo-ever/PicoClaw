"""Central tool registration, schema exposure, validation, and execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .models import ToolCall, ToolResult

ToolHandler = Callable[..., str]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult(call.id, call.name, f"unknown tool: {call.name}", is_error=True)

        try:
            self._validate_required_arguments(tool, call.arguments)
            content = str(tool.handler(**call.arguments))
        # Tool handlers are an execution boundary: filesystem, subprocess, and
        # remote adapters can raise unrelated exceptions. Normalize them into a
        # ToolResult so one failed tool cannot crash the whole Agent Loop.
        except Exception as exc:  # noqa: BLE001
            return ToolResult(call.id, call.name, f"{type(exc).__name__}: {exc}", is_error=True)

        return ToolResult(call.id, call.name, content)

    @staticmethod
    def _validate_required_arguments(tool: Tool, arguments: dict[str, Any]) -> None:
        required = tool.parameters.get("required", [])
        missing = [name for name in required if name not in arguments]
        if missing:
            names = ", ".join(missing)
            raise ValueError(f"missing required arguments: {names}")


def build_read_only_registry(workspace) -> ToolRegistry:
    """Create the first safe tool set used by M1."""

    registry = ToolRegistry()
    registry.register(
        Tool(
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
    )
    return registry
