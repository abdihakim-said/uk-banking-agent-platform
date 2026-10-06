"""Connects a domain agent to its MCP server, and only its MCP server.

The model sees each tool's schema minus the agent-controlled parameters (``context_token`` and ``dry_run``).
Whether a tool changes state is read from the server's schema (it has a ``dry_run`` parameter), not from a
hard-coded list, so a new write tool can't be added without going through confirmation.
"""

from __future__ import annotations

import copy
import json
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from mcp import Client
from mcp.server.mcpserver import MCPServer
from opentelemetry import trace

from bank_assistant.llm.types import ToolSpec
from bank_assistant.mcp_servers.common import HIDDEN_PARAMETERS
from bank_assistant.observability import metrics

tracer = trace.get_tracer("bank_assistant.tools")


@dataclass(frozen=True)
class ToolOutcome:
    data: dict[str, Any] | None
    text: str
    is_error: bool


class ToolNotAllowedError(Exception):
    pass


class McpToolbox:
    def __init__(self, domain: str, target: str | MCPServer, timeout_seconds: float = 15.0) -> None:
        self.domain = domain
        self.target = target
        self.timeout = timeout_seconds
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._specs: dict[str, ToolSpec] = {}
        self._write_tools: set[str] = set()

    async def __aenter__(self) -> McpToolbox:
        self._stack = AsyncExitStack()
        self._client = await self._stack.enter_async_context(Client(self.target, read_timeout_seconds=self.timeout))
        listed = await self._client.list_tools()
        for tool in listed.tools:
            schema = copy.deepcopy(tool.input_schema)
            props: dict[str, Any] = schema.get("properties", {})
            if "dry_run" in props:
                self._write_tools.add(tool.name)
            for hidden in HIDDEN_PARAMETERS:
                props.pop(hidden, None)
            schema["required"] = [r for r in schema.get("required", []) if r not in HIDDEN_PARAMETERS]
            schema.pop("title", None)
            self._specs[tool.name] = ToolSpec(tool.name, tool.description or "", schema)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()

    @property
    def specs(self) -> list[ToolSpec]:
        return list(self._specs.values())

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self._specs)

    def is_write(self, name: str) -> bool:
        return name in self._write_tools

    async def call(self, name: str, arguments: dict[str, Any], context_token: str,
                   dry_run: bool | None = None) -> ToolOutcome:
        if self._client is None:
            raise RuntimeError("toolbox used outside 'async with'")
        if name not in self._specs:
            raise ToolNotAllowedError(f"{name} is not available to the {self.domain} agent")
        args = {k: v for k, v in arguments.items() if k not in HIDDEN_PARAMETERS}
        args["context_token"] = context_token
        if self.is_write(name):
            args["dry_run"] = True if dry_run is None else dry_run
        started = time.perf_counter()
        with tracer.start_as_current_span(f"mcp.{name}") as span:
            span.set_attribute("mcp.server", self.domain)
            span.set_attribute("mcp.tool", name)
            span.set_attribute("mcp.dry_run", bool(args.get("dry_run", False)))
            result = await self._client.call_tool(name, args)
            span.set_attribute("mcp.is_error", result.is_error)
        metrics.TOOL_LATENCY.labels(self.domain, name).observe(time.perf_counter() - started)
        metrics.TOOL_CALLS.labels(self.domain, name, "error" if result.is_error else "ok").inc()
        text = "\n".join(getattr(block, "text", "") for block in result.content).strip()
        data = result.structured_content if isinstance(result.structured_content, dict) else None
        if data is None and not result.is_error:
            try:
                parsed = json.loads(text)
                data = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                data = None
        return ToolOutcome(data=data, text=text if result.is_error else json.dumps(data) if data else text,
                           is_error=result.is_error)
