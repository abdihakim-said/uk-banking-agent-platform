"""Edge gateway: the only public entry point.

Per request: authenticate (OIDC) → rate-limit (customer + IP) → validate size → claim the session for this
customer → take the session lock → redact PII → call the agent service → render placeholders as masked values.
Raw customer text never leaves this process: the agent service and the LLMs only see redacted text.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from bank_assistant.config import Settings, get_settings
from bank_assistant.core.session_store import SessionOwnershipError, SessionStore
from bank_assistant.gateway.auth import AuthError, JwtVerifier, Principal
from bank_assistant.gateway.rate_limit import RateLimiter, SessionLock
from bank_assistant.logging import configure_logging, get_logger
from bank_assistant.observability import metrics
from bank_assistant.observability.tracing import setup_tracing
from bank_assistant.security.redactor import PiiVault, Redactor, default_name_detector

log = get_logger(__name__)
MAX_BODY_BYTES = 16 * 1024


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str | None = Field(default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    awaiting_confirmation: bool
    handoff: bool
    route: str


def create_app(settings: Settings | None = None, redis: Redis | None = None, verifier: JwtVerifier | None = None,
               agent_client: httpx.AsyncClient | None = None, redactor: Redactor | None = None) -> FastAPI:
    settings = settings or get_settings()
    deps: dict[str, Any] = {"redis": redis, "verifier": verifier, "agent": agent_client, "redactor": redactor}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if deps["redis"] is None:
            configure_logging(settings.log_level, "gateway")
            setup_tracing("gateway", settings.otel_exporter_otlp_endpoint)
            deps["redis"] = Redis.from_url(settings.redis_url)
        if deps["verifier"] is None:
            deps["verifier"] = JwtVerifier.from_settings(settings.oidc_issuer, settings.oidc_audience,
                                                         settings.oidc_required_scope, settings.oidc_jwks_url,
                                                         settings.oidc_dev_public_key_path)
        if deps["agent"] is None:
            deps["agent"] = httpx.AsyncClient(base_url=settings.agent_service_url, timeout=120.0)
        if deps["redactor"] is None:
            deps["redactor"] = Redactor(default_name_detector())
        yield
        if agent_client is None:
            await deps["agent"].aclose()

    app = FastAPI(title="UK Banking Assistant Gateway", lifespan=lifespan)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        length = request.headers.get("content-length")
        if length is not None and int(length) > MAX_BODY_BYTES:
            metrics.GATEWAY_REQUESTS.labels("too_large").inc()
            return JSONResponse({"detail": "request too large"}, status_code=413)
        response: Response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    def fail(status: int, detail: str, outcome: str, headers: dict[str, str] | None = None) -> HTTPException:
        metrics.GATEWAY_REQUESTS.labels(outcome).inc()
        return HTTPException(status_code=status, detail=detail, headers=headers)

    @app.post("/v1/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest, request: Request,
                   authorization: Annotated[str | None, Header()] = None) -> ChatResponse:
        redis_client: Redis = deps["redis"]
        try:
            principal: Principal = deps["verifier"].verify(authorization)
        except AuthError as exc:
            raise fail(exc.status, exc.detail, "auth_failed",
                       {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None) from exc

        limiter = RateLimiter(redis_client)
        client_ip = request.client.host if request.client else "unknown"
        for scope, key, limit in (("customer", principal.customer_id, settings.rate_limit_per_minute),
                                  ("ip", client_ip, settings.rate_limit_per_ip_per_minute)):
            allowed, retry_after = await limiter.hit(scope, key, limit)
            if not allowed:
                metrics.RATE_LIMITED.labels(scope).inc()
                raise fail(429, "too many requests", "rate_limited", {"Retry-After": str(retry_after)})

        if len(body.message) > settings.max_message_chars:
            raise fail(422, f"message is longer than {settings.max_message_chars} characters", "invalid")

        store = SessionStore(redis_client, settings.session_ttl_seconds, settings.history_max_messages)
        session_id = body.session_id or secrets.token_urlsafe(18)
        try:
            await store.claim(session_id, principal.customer_id)
        except SessionOwnershipError as exc:
            raise fail(403, "session belongs to another user", "session_forbidden") from exc

        lock = SessionLock(redis_client)
        if not await lock.acquire(session_id):
            raise fail(409, "the previous message in this session is still being processed", "busy")
        try:
            vault = PiiVault(await store.load_vault(session_id))
            redacted = deps["redactor"].redact(body.message, vault, principal.names)
            for placeholder in vault.new_entries:
                metrics.PII_REDACTIONS.labels(placeholder[1:].rsplit("_", 1)[0], "gateway").inc()
            await store.save_vault(session_id, vault.new_entries)

            try:
                resp = await deps["agent"].post(
                    "/internal/v1/turns",
                    headers={"Authorization": f"Bearer {settings.internal_service_token.get_secret_value()}"},
                    json={"session_id": session_id, "customer_id": principal.customer_id, "message": redacted,
                          "known_names": principal.names},
                )
            except httpx.HTTPError as exc:
                log.error("agent_unreachable", error=type(exc).__name__)
                raise fail(503, "assistant unavailable", "agent_unavailable") from exc
            if resp.status_code != 200:
                log.error("agent_error", status=resp.status_code)
                raise fail(502, "assistant error", "agent_error")
            turn = resp.json()

            vault = PiiVault(await store.load_vault(session_id))  # the agent may have added entries
            metrics.GATEWAY_REQUESTS.labels("ok").inc()
            log.info("chat_turn", session_id=session_id, route=turn["route"], outcome=turn["outcome"],
                     cost_usd=turn["cost_usd"])
            return ChatResponse(session_id=session_id, reply=vault.render_for_customer(turn["reply"]),
                                awaiting_confirmation=turn["awaiting_confirmation"], handoff=turn["handoff"],
                                route=turn["route"])
        finally:
            await lock.release(session_id)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
