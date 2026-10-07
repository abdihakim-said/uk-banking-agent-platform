"""Edge gateway: the only public entry point.

Per request: authenticate (OIDC) → rate-limit (customer + IP) → validate size → claim the session for this
customer → take the session lock → redact PII → call the agent service → render placeholders as masked values.
Raw customer text never leaves this process: the agent service and the LLMs only see redacted text.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from opentelemetry.propagate import inject
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from bank_assistant.config import Settings, get_settings
from bank_assistant.core.audit import AuditLog
from bank_assistant.core.redis_client import async_redis
from bank_assistant.core.session_store import SessionOwnershipError, SessionStore
from bank_assistant.core.tls import client_context
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
    redacted: list[str] = Field(default_factory=list, description="Kinds of personal data hidden from the AI")


def create_app(settings: Settings | None = None, redis: Redis | None = None, verifier: JwtVerifier | None = None,
               agent_client: httpx.AsyncClient | None = None, redactor: Redactor | None = None,
               audit: AuditLog | None = None) -> FastAPI:
    settings = settings or get_settings()
    deps: dict[str, Any] = {"redis": redis, "verifier": verifier, "agent": agent_client, "redactor": redactor,
                            "audit": audit}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if deps["redis"] is None:
            configure_logging(settings.log_level, "gateway")
            setup_tracing("gateway", settings.otel_exporter_otlp_endpoint)
            deps["redis"] = async_redis(settings)
        if deps["audit"] is None:
            deps["audit"] = AuditLog(deps["redis"], settings.audit_hmac_key.get_secret_value(), "gateway")
        if deps["verifier"] is None:
            deps["verifier"] = JwtVerifier.from_settings(settings.oidc_issuer, settings.oidc_audience,
                                                         settings.oidc_required_scope, settings.oidc_jwks_url,
                                                         settings.oidc_dev_public_key_path,
                                                         ssl_context=client_context(settings, trust_public_cas=True))
        if deps["agent"] is None:
            deps["agent"] = httpx.AsyncClient(base_url=settings.agent_service_url, timeout=120.0,
                                              verify=client_context(settings) or True)
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
        started = time.perf_counter()
        client_ip = request.client.host if request.client else "unknown"
        audit: AuditLog | None = deps["audit"]
        try:
            principal: Principal = deps["verifier"].verify(authorization)
        except AuthError as exc:
            if audit:
                await audit.record("auth_failed", outcome="denied", reason=exc.detail, ip=client_ip)
            raise fail(exc.status, exc.detail, "auth_failed",
                       {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None) from exc

        limiter = RateLimiter(redis_client)
        for scope, key, limit in (("customer", principal.customer_id, settings.rate_limit_per_minute),
                                  ("ip", client_ip, settings.rate_limit_per_ip_per_minute)):
            allowed, retry_after = await limiter.hit(scope, key, limit)
            if not allowed:
                metrics.RATE_LIMITED.labels(scope).inc()
                if audit:
                    await audit.record("rate_limited", outcome="denied", actor=principal.customer_id, scope=scope,
                                       ip=client_ip)
                raise fail(429, "too many requests", "rate_limited", {"Retry-After": str(retry_after)})

        if len(body.message) > settings.max_message_chars:
            raise fail(422, f"message is longer than {settings.max_message_chars} characters", "invalid")

        store = SessionStore(redis_client, settings.session_ttl_seconds, settings.history_max_messages)
        session_id = body.session_id or secrets.token_urlsafe(18)
        try:
            await store.claim(session_id, principal.customer_id)
        except SessionOwnershipError as exc:
            if audit:
                await audit.record("session_access_denied", outcome="denied", actor=principal.customer_id,
                                   session_id=session_id, ip=client_ip)
            raise fail(403, "session belongs to another user", "session_forbidden") from exc

        lock = SessionLock(redis_client)
        if not await lock.acquire(session_id):
            raise fail(409, "the previous message in this session is still being processed", "busy")
        try:
            vault = PiiVault(await store.load_vault(session_id))
            hidden = sorted({f.type.value for f in deps["redactor"].find(body.message, principal.names)})
            redacted = deps["redactor"].redact(body.message, vault, principal.names)
            for placeholder in vault.new_entries:
                metrics.PII_REDACTIONS.labels(placeholder[1:].rsplit("_", 1)[0], "gateway").inc()
            await store.save_vault(session_id, vault.new_entries)

            headers = {"Authorization": f"Bearer {settings.internal_service_token.get_secret_value()}"}
            inject(headers)  # W3C traceparent, so the agent's spans join this request's trace
            try:
                resp = await deps["agent"].post(
                    "/internal/v1/turns",
                    headers=headers,
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
            metrics.GATEWAY_LATENCY.observe(time.perf_counter() - started)
            if audit:
                await audit.record("chat_turn", outcome=turn["outcome"], actor=principal.customer_id,
                                   session_id=session_id, route=turn["route"], pii_types_hidden=hidden)
            log.info("chat_turn", session_id=session_id, route=turn["route"], outcome=turn["outcome"],
                     cost_usd=turn["cost_usd"])
            return ChatResponse(session_id=session_id, reply=vault.render_for_customer(turn["reply"]),
                                awaiting_confirmation=turn["awaiting_confirmation"], handoff=turn["handoff"],
                                route=turn["route"], redacted=hidden)
        finally:
            await lock.release(session_id)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness: the process is serving requests."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Readiness: Redis and the agent service are reachable. Kubernetes only routes traffic when this is 200."""
        checks: dict[str, str] = {}
        try:
            await deps["redis"].ping()
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = type(exc).__name__
        try:
            r = await deps["agent"].get("/healthz", timeout=2.0)
            checks["agent"] = "ok" if r.status_code == 200 else f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            checks["agent"] = type(exc).__name__
        ready = all(v == "ok" for v in checks.values())
        return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)

    return app
