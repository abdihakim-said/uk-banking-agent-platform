"""Signed customer-context tokens.

The LLM never chooses whose data a tool touches. The agent service mints a short-lived token for the
authenticated customer and injects it into every MCP tool call; the MCP server verifies the signature
and takes the customer id from the token, never from model-written arguments.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass


class InvalidContextTokenError(Exception):
    """The token is malformed, tampered with or expired."""


@dataclass(frozen=True)
class ContextClaims:
    customer_id: str
    session_id: str
    expires_at: int


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def issue_context_token(
    customer_id: str, session_id: str, secret: str, ttl_seconds: int = 120, now: float | None = None
) -> str:
    issued = int(now if now is not None else time.time())
    payload = _b64(
        json.dumps({"v": 1, "cid": customer_id, "sid": session_id, "exp": issued + ttl_seconds}).encode()
    )
    signature = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def verify_context_token(token: str, secret: str, now: float | None = None) -> ContextClaims:
    try:
        payload, signature = token.split(".", 1)
    except ValueError as exc:
        raise InvalidContextTokenError("malformed token") from exc
    expected = _b64(hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, signature):
        raise InvalidContextTokenError("bad signature")
    try:
        data = json.loads(_unb64(payload))
        claims = ContextClaims(customer_id=str(data["cid"]), session_id=str(data["sid"]), expires_at=int(data["exp"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise InvalidContextTokenError("malformed payload") from exc
    if claims.expires_at < int(now if now is not None else time.time()):
        raise InvalidContextTokenError("expired")
    return claims
