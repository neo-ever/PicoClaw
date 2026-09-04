"""Controlled MCP stdio adapter backed by the official MCP Python SDK."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

from .tools import RiskLevel, Tool, ToolRegistry

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")
ClientFactory = Callable[
    [StdioServerParameters, float],
    AbstractAsyncContextManager[Any],
]


class MCPAdapterError(RuntimeError):
    """Raised when discovery, transport, or a remote MCP tool fails."""


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    allowed_tools: frozenset[str] = frozenset()
    default_risk: RiskLevel = RiskLevel.EXECUTE
    risk_by_tool: Mapping[str, RiskLevel] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    max_result_chars: int = 30_000

    def __post_init__(self) -> None:
        if not _SAFE_NAME.fullmatch(self.name):
            raise ValueError("MCP server name may contain only letters, digits, '_' and '-'")
        if not self.command.strip():
            raise ValueError("MCP stdio command cannot be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("MCP timeout must be positive")
        if self.max_result_chars < 100:
            raise ValueError("MCP max_result_chars must be at least 100")
        unknown_risks = set(self.risk_by_tool) - set(self.allowed_tools)
        if unknown_risks:
            raise ValueError(f"risk configured for non-allowlisted tools: {sorted(unknown_risks)}")
        if self.cwd is not None:
            resolved = self.cwd.resolve()
            if not resolved.is_dir():
                raise ValueError(f"MCP cwd is not a directory: {resolved}")
        for key, value in self.env.items():
            if not key or "=" in key or "\x00" in key + value:
                raise ValueError(f"invalid MCP environment entry: {key!r}")


@dataclass(frozen=True, slots=True)
class MCPToolDescriptor:
    exposed_name: str
    remote_name: str
    description: str
    input_schema: dict[str, Any]
    risk: RiskLevel


class MCPToolAdapter:
    """Discover an operator-approved subset and expose it through ToolRegistry."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self.config = config
        self.client_factory = client_factory or self._default_client

    def discover(self) -> tuple[MCPToolDescriptor, ...]:
        return self._run(self._discover())

    def register_tools(self, registry: ToolRegistry) -> tuple[MCPToolDescriptor, ...]:
        descriptors = self.discover()
        for descriptor in descriptors:
            registry.register(
                Tool(
                    name=descriptor.exposed_name,
                    description=(f"MCP server '{self.config.name}': {descriptor.description}"),
                    parameters=descriptor.input_schema,
                    handler=self._handler_for(descriptor.remote_name),
                    risk=descriptor.risk,
                )
            )
        return descriptors

    def call(self, remote_name: str, arguments: dict[str, Any]) -> str:
        if remote_name not in self.config.allowed_tools:
            raise MCPAdapterError(f"MCP tool is not allowlisted: {remote_name}")
        return self._run(self._call(remote_name, arguments))

    async def _discover(self) -> tuple[MCPToolDescriptor, ...]:
        discovered: dict[str, Any] = {}
        try:
            async with self._new_client() as client:
                cursor: str | None = None
                while True:
                    result = await client.list_tools(cursor=cursor)
                    for tool in result.tools:
                        discovered[tool.name] = tool
                    cursor = getattr(result, "next_cursor", None)
                    if not cursor:
                        break
        except Exception as exc:
            raise MCPAdapterError(
                f"failed to discover MCP server '{self.config.name}': {type(exc).__name__}: {exc}"
            ) from exc

        missing = sorted(set(self.config.allowed_tools) - set(discovered))
        if missing:
            raise MCPAdapterError(f"allowlisted MCP tools were not advertised: {missing}")

        descriptors: list[MCPToolDescriptor] = []
        exposed_names: set[str] = set()
        for remote_name in sorted(self.config.allowed_tools):
            remote_tool = discovered[remote_name]
            exposed_name = self._exposed_name(remote_name)
            if exposed_name in exposed_names:
                raise MCPAdapterError(f"MCP exposed tool name collision: {exposed_name}")
            exposed_names.add(exposed_name)
            schema = dict(getattr(remote_tool, "input_schema", {}) or {})
            if schema.get("type", "object") != "object":
                raise MCPAdapterError(f"MCP tool input schema must be an object: {remote_name}")
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            descriptors.append(
                MCPToolDescriptor(
                    exposed_name=exposed_name,
                    remote_name=remote_name,
                    description=getattr(remote_tool, "description", "") or remote_name,
                    input_schema=schema,
                    risk=self.config.risk_by_tool.get(remote_name, self.config.default_risk),
                )
            )
        return tuple(descriptors)

    async def _call(self, remote_name: str, arguments: dict[str, Any]) -> str:
        try:
            async with self._new_client() as client:
                result = await client.call_tool(remote_name, arguments)
        except Exception as exc:
            raise MCPAdapterError(
                f"MCP tool '{remote_name}' transport failed: {type(exc).__name__}: {exc}"
            ) from exc

        rendered = self._render_result(result)
        if len(rendered) > self.config.max_result_chars:
            rendered = rendered[: self.config.max_result_chars].rstrip() + "…<truncated>"
        if getattr(result, "is_error", False):
            raise MCPAdapterError(f"MCP tool '{remote_name}' returned an error: {rendered}")
        return rendered

    def _new_client(self) -> AbstractAsyncContextManager[Any]:
        params = StdioServerParameters(
            command=self.config.command,
            args=list(self.config.args),
            env=dict(self.config.env) or None,
            cwd=str(self.config.cwd.resolve()) if self.config.cwd is not None else None,
        )
        return self.client_factory(params, self.config.timeout_seconds)

    @staticmethod
    def _default_client(
        params: StdioServerParameters,
        timeout_seconds: float,
    ) -> AbstractAsyncContextManager[Any]:
        return Client(params, read_timeout_seconds=timeout_seconds)

    @staticmethod
    def _run(coroutine: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        coroutine.close()
        raise MCPAdapterError("synchronous MCP adapter cannot run inside an active event loop")

    def _handler_for(self, remote_name: str) -> Callable[..., str]:
        def handler(**arguments: Any) -> str:
            return self.call(remote_name, arguments)

        return handler

    def _exposed_name(self, remote_name: str) -> str:
        safe_remote = re.sub(r"[^a-zA-Z0-9_-]", "_", remote_name)
        candidate = f"mcp__{self.config.name}__{safe_remote}"
        if len(candidate) <= 64:
            return candidate
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:8]
        return f"{candidate[:55]}_{digest}"

    @staticmethod
    def _render_result(result: Any) -> str:
        chunks: list[str] = []
        for item in getattr(result, "content", ()):
            kind = getattr(item, "type", type(item).__name__)
            if kind == "text":
                chunks.append(str(getattr(item, "text", "")))
            elif kind in {"image", "audio"}:
                mime_type = getattr(item, "mime_type", "unknown")
                chunks.append(f"<{kind} content omitted; mime_type={mime_type}>")
            elif kind == "resource_link":
                chunks.append(f"<resource link: {getattr(item, 'uri', 'unknown')}>")
            elif kind == "resource":
                resource = getattr(item, "resource", None)
                text = getattr(resource, "text", None)
                chunks.append(str(text) if text is not None else "<binary resource omitted>")
            else:
                chunks.append(f"<{kind} content omitted>")
        structured = getattr(result, "structured_content", None)
        if structured is not None and not chunks:
            chunks.append(json.dumps(structured, ensure_ascii=False, sort_keys=True))
        return "\n".join(chunk for chunk in chunks if chunk).strip() or "<empty MCP result>"
