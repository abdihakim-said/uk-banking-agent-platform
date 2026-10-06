"""Agent service: internal HTTP API that runs one turn of the graph.

It only accepts calls from the gateway (shared service token; use mTLS or a service mesh in production) and
only ever receives text the gateway has already redacted.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from bank_assistant.agents.graph import AgentDeps, AgentEngine
from bank_assistant.config import Settings, get_settings
from bank_assistant.core.session_store import SessionStore
from bank_assistant.llm.factory import build_router
from bank_assistant.logging import configure_logging
from bank_assistant.observability import metrics
from bank_assistant.observability.cost import CostTracker
from bank_assistant.observability.tracing import setup_tracing
from bank_assistant.security.redactor import Redactor, default_name_detector


class TurnRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    customer_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)
    known_names: list[str] = Field(default_factory=list, max_length=5)


class TurnResponse(BaseModel):
    reply: str
    route: str
    outcome: str
    handoff: bool
    awaiting_confirmation: bool
    provider: str | None
    cost_usd: Decimal
    trajectory: list[dict[str, Any]]


def create_app(engine: AgentEngine | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    state: dict[str, Any] = {"engine": engine}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if state["engine"] is None:
            configure_logging(settings.log_level, "agent-service")
            setup_tracing("agent-service", settings.otel_exporter_otlp_endpoint)
            metrics.serve_metrics(settings.metrics_port)
            redis = Redis.from_url(settings.redis_url)
            state["engine"] = AgentEngine(AgentDeps(
                router=build_router(settings),
                store=SessionStore(redis, settings.session_ttl_seconds, settings.history_max_messages),
                costs=CostTracker(redis, session_budget=settings.session_budget_usd,
                                  customer_daily_budget=settings.customer_daily_budget_usd),
                redactor=Redactor(default_name_detector()),
                mcp_targets={"accounts": settings.mcp_accounts_url, "transactions": settings.mcp_transactions_url,
                             "service": settings.mcp_service_url},
                context_secret=settings.context_token_secret.get_secret_value(),
                context_ttl_seconds=settings.context_token_ttl_seconds,
            ))
        yield

    app = FastAPI(title="Agent service", lifespan=lifespan, docs_url=None, redoc_url=None)

    def require_service_token(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = f"Bearer {settings.internal_service_token.get_secret_value()}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="unauthorised")

    @app.post("/internal/v1/turns", response_model=TurnResponse, dependencies=[Depends(require_service_token)])
    async def run_turn(body: TurnRequest) -> TurnResponse:
        engine: AgentEngine = state["engine"]
        result = await engine.run_turn(body.session_id, body.customer_id, body.message, body.known_names)
        return TurnResponse(reply=result.reply, route=result.route, outcome=result.outcome, handoff=result.handoff,
                            awaiting_confirmation=result.awaiting_confirmation, provider=result.provider,
                            cost_usd=result.cost_usd, trajectory=result.trajectory)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness: the process is serving requests."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Readiness: Redis and every MCP server answer. Without them a turn can't complete."""
        engine: AgentEngine = state["engine"]
        checks: dict[str, str] = {}
        try:
            await engine.deps.store.redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = type(exc).__name__
        async with httpx.AsyncClient(timeout=2.0) as client:
            for domain, target in engine.deps.mcp_targets.items():
                if not isinstance(target, str):
                    checks[f"mcp_{domain}"] = "ok"  # in-process server (tests)
                    continue
                try:
                    r = await client.get(target.rsplit("/mcp", 1)[0] + "/healthz")
                    checks[f"mcp_{domain}"] = "ok" if r.status_code == 200 else f"HTTP {r.status_code}"
                except httpx.HTTPError as exc:
                    checks[f"mcp_{domain}"] = type(exc).__name__
        ready = all(v == "ok" for v in checks.values())
        return JSONResponse({"ready": ready, "checks": checks}, status_code=200 if ready else 503)

    return app

