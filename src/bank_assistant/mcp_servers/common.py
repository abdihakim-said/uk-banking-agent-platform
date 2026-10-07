"""Shared helpers for the MCP servers.

Conventions every tool follows:
  * ``context_token`` (first parameter) is injected by the agent service, never written by the model.
    It is verified here and is the only source of the customer id.
  * State-changing tools take ``dry_run``, also agent-controlled. ``dry_run=True`` validates the request and
    returns a summary without changing anything; the agent shows that summary to the customer and calls again
    with ``dry_run=False`` only after the customer confirms.
  * Failures the model can act on raise ``ToolError`` with a safe message. Not-found and not-yours are the
    same error.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse

from bank_assistant.config import get_settings
from bank_assistant.core.bank_data import BusinessRuleError, CoreBankingStore, NotFoundError, ServiceRequest
from bank_assistant.core.context_token import InvalidContextTokenError, verify_context_token

HIDDEN_PARAMETERS = frozenset({"context_token", "dry_run"})
"""Parameters the agent service fills in itself and removes from the schema the model sees."""

P = ParamSpec("P")
R = TypeVar("R")


def customer_from(context_token: str, secret: str) -> str:
    try:
        return verify_context_token(context_token, secret).customer_id
    except InvalidContextTokenError as exc:
        raise ToolError("not authorised: invalid or expired customer context") from exc


def domain_errors(fn: Callable[P, R]) -> Callable[P, R]:
    """Turn domain exceptions into ToolErrors the model can read and recover from."""

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except NotFoundError as exc:
            raise ToolError(f"{exc}. Use the list tools to find valid references.") from exc
        except BusinessRuleError as exc:
            raise ToolError(f"request not allowed: {exc}") from exc

    return wrapper


def request_result(request: ServiceRequest) -> dict[str, Any]:
    if request.status == "preview":
        return {"status": "preview", "action": request.kind, "details": request.details}
    return {"status": request.status, "reference": request.reference, "action": request.kind,
            "details": request.details}


def shared_store() -> CoreBankingStore:
    """Every MCP server process seeds the same synthetic reference data (same seed). Runtime state (disputes, card
    replacements, requests) is in Redis, so all replicas of all three servers agree on it."""
    from redis import Redis

    from bank_assistant.core.bank_state import RedisBankState

    return CoreBankingStore(state=RedisBankState(Redis.from_url(get_settings().redis_url)))


def secret() -> str:
    return get_settings().context_token_secret.get_secret_value()


def serve(server: MCPServer) -> None:
    """Run an MCP server over streamable HTTP with tracing, JSON logs, a metrics port and /healthz.

    The MCP SDK already carries W3C trace context in each request's ``_meta`` and continues the trace on the server,
    so with an exporter configured, tool calls appear inside the agent's trace in Jaeger.
    """
    from bank_assistant.logging import configure_logging
    from bank_assistant.observability.metrics import serve_metrics
    from bank_assistant.observability.tracing import setup_tracing

    settings = get_settings()
    name = server.name or "mcp-server"
    configure_logging(settings.log_level, name)
    setup_tracing(name, settings.otel_exporter_otlp_endpoint)
    serve_metrics(settings.metrics_port)

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)  # type: ignore[untyped-decorator]
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    server.run("streamable-http", **http_options())


def http_options() -> dict[str, Any]:
    return {
        "host": os.environ.get("MCP_HOST", "127.0.0.1"),
        "port": int(os.environ.get("MCP_PORT", "9101")),
        "stateless_http": True,
        "json_response": True,
    }
