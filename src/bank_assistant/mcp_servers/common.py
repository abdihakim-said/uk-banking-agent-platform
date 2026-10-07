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

from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from mcp.server.mcpserver.exceptions import ToolError

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
    from bank_assistant.core.bank_state import RedisBankState
    from bank_assistant.core.redis_client import sync_redis

    return CoreBankingStore(state=RedisBankState(sync_redis(get_settings())))


def secret() -> str:
    return get_settings().context_token_secret.get_secret_value()
