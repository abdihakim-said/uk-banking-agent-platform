"""Start any service with the same production setup.

    python -m bank_assistant.serve gateway|agent|demo-idp|accounts-mcp|transactions-mcp|service-mcp

Every service gets JSON logs, tracing, the internal ops port (/metrics, /healthz, /readyz on BANK_METRICS_PORT) and,
when BANK_TLS_ENABLED=true, an API port that requires a client certificate from the internal CA.
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import uvicorn

from bank_assistant.config import Settings, get_settings
from bank_assistant.core.ops_server import Readiness, http_check, redis_check, serve_ops
from bank_assistant.core.redis_client import sync_redis
from bank_assistant.core.tls import client_context, server_kwargs
from bank_assistant.logging import configure_logging
from bank_assistant.observability.tracing import setup_tracing

DEFAULT_PORTS = {"gateway": 8000, "agent": 8001, "demo-idp": 8000,
                 "accounts-mcp": 9101, "transactions-mcp": 9102, "service-mcp": 9103}
GRACE_SECONDS = {"gateway": 45, "agent": 75}


def _mcp_app(name: str) -> Any:
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from bank_assistant.mcp_servers.accounts_server import build_accounts_server
    from bank_assistant.mcp_servers.common import secret, shared_store
    from bank_assistant.mcp_servers.service_server import build_service_server
    from bank_assistant.mcp_servers.transactions_server import build_transactions_server

    builders = {"accounts-mcp": build_accounts_server, "transactions-mcp": build_transactions_server,
                "service-mcp": build_service_server}
    server = builders[name](shared_store(), secret())

    @server.custom_route("/healthz", methods=["GET"], include_in_schema=False)  # type: ignore[untyped-decorator]
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    # Stateless: any replica can serve any request, because runtime state lives in Redis.
    return server.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")  # noqa: S104


def _app(name: str, settings: Settings) -> Any:
    if name == "gateway":
        from bank_assistant.gateway.app import create_app

        return create_app(settings)
    if name == "agent":
        from bank_assistant.services.agent_api import create_app as create_agent

        return create_agent(settings=settings)
    if name == "demo-idp":
        from bank_assistant.demo_idp.app import create_app as create_idp

        return create_idp(settings)
    return _mcp_app(name)


def _readiness(name: str, settings: Settings) -> Readiness:
    """What 'ready' means for each service: its own dependencies answer, over the same mTLS path real calls use."""
    ctx = client_context(settings)
    redis = sync_redis(settings) if name != "demo-idp" else None
    deps: dict[str, str] = {}
    if name == "gateway":
        deps = {"agent": f"{settings.agent_service_url}/healthz"}
    elif name == "agent":
        deps = {f"mcp_{d}": url.rsplit("/mcp", 1)[0] + "/healthz" for d, url in (
            ("accounts", settings.mcp_accounts_url), ("transactions", settings.mcp_transactions_url),
            ("service", settings.mcp_service_url))}

    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="readyz")

    def check() -> dict[str, str]:
        # In parallel, with short timeouts, so the whole check fits well inside the probe's 3-second timeout even
        # when every dependency is down.
        futures = {dep: pool.submit(http_check, url, ctx, 1.5) for dep, url in deps.items()}
        if redis is not None:
            futures["redis"] = pool.submit(redis_check, redis)
        return {dep: f.result() for dep, f in futures.items()}

    return check


def run(name: str, port: int | None = None) -> None:
    if name not in DEFAULT_PORTS:
        raise SystemExit(f"unknown service {name!r}; choose one of {', '.join(DEFAULT_PORTS)}")
    settings = get_settings()
    configure_logging(settings.log_level, name)
    setup_tracing(name, settings.otel_exporter_otlp_endpoint)
    serve_ops(settings.metrics_port, _readiness(name, settings))
    uvicorn.run(_app(name, settings), host="0.0.0.0",  # noqa: S104 - container network
                port=port or int(os.environ.get("PORT", DEFAULT_PORTS[name])),
                timeout_graceful_shutdown=GRACE_SECONDS.get(name, 10), server_header=False,
                log_level="warning", **server_kwargs(settings))


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        raise SystemExit(__doc__)
    run(args[0])

if __name__ == "__main__":
    main()
