"""Mutual TLS with real servers on localhost: a valid client certificate is required on every internal hop."""

from __future__ import annotations

import importlib.util
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from bank_assistant.agents.toolbox import McpToolbox
from bank_assistant.config import Settings
from bank_assistant.core.bank_data import CoreBankingStore
from bank_assistant.core.context_token import issue_context_token
from bank_assistant.core.tls import client_context, server_kwargs
from bank_assistant.mcp_servers.accounts_server import build_accounts_server

_spec = importlib.util.spec_from_file_location("dev_certs", Path(__file__).parents[1] / "scripts" / "dev_certs.py")
dev_certs = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(dev_certs)  # type: ignore[union-attr]
SECRET = "s" * 40


def tls_settings(certs: Path, service: str) -> Settings:
    d = certs / service
    return Settings(environment="test", tls_enabled=True, tls_cert_file=str(d / "tls.crt"),  # type: ignore[arg-type]
                    tls_key_file=str(d / "tls.key"), tls_ca_file=str(d / "ca.crt"))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def serve(app: Any, settings: Settings) -> Iterator[str]:
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                           **server_kwargs(settings)))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"https://localhost:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def certs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("certs")
    dev_certs.generate(out, "bank-uk-banking-agent-platform", "bank")
    return out


@pytest.fixture(scope="module")
def agent_url(certs: Path) -> Iterator[str]:
    app = FastAPI()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    yield from serve(app, tls_settings(certs, "agent"))


def test_caller_with_a_client_certificate_gets_through(certs: Path, agent_url: str) -> None:
    ctx = client_context(tls_settings(certs, "gateway"))
    assert httpx.get(f"{agent_url}/healthz", verify=ctx).json() == {"status": "ok"}


def test_caller_without_a_client_certificate_is_refused(certs: Path, agent_url: str) -> None:
    trusts_ca_only = ssl.create_default_context(cafile=str(certs / "ca.crt"))
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{agent_url}/healthz", verify=trusts_ca_only)


def test_certificate_from_another_ca_is_refused(certs: Path, agent_url: str, tmp_path: Path) -> None:
    dev_certs.generate(tmp_path, "x", "y")  # a different CA, e.g. an attacker's
    rogue = client_context(tls_settings(tmp_path, "gateway"))
    rogue.load_verify_locations(cafile=str(certs / "ca.crt"))  # trust our server, present the rogue cert
    with pytest.raises(httpx.HTTPError):
        httpx.get(f"{agent_url}/healthz", verify=rogue)


async def test_agent_calls_mcp_server_over_mtls(certs: Path) -> None:
    server = build_accounts_server(CoreBankingStore(), SECRET)
    app = server.streamable_http_app(stateless_http=True, json_response=True, host="127.0.0.1")
    gen = serve(app, tls_settings(certs, "accounts-mcp"))
    base = next(gen)
    try:
        token = issue_context_token("C001", "sess-tls", SECRET)
        ctx = client_context(tls_settings(certs, "agent"))
        async with McpToolbox("accounts", f"{base}/mcp", tls_context=ctx) as toolbox:
            balance = await toolbox.call("get_balance", {}, token)
        assert balance.data is not None and balance.data["balance"] == "1842.37"

        no_cert = ssl.create_default_context(cafile=str(certs / "ca.crt"))
        with pytest.raises(BaseException):  # noqa: B017 - the MCP client wraps the TLS failure in an ExceptionGroup
            async with McpToolbox("accounts", f"{base}/mcp", tls_context=no_cert) as toolbox:
                await toolbox.call("get_balance", {}, token)
    finally:
        next(gen, None)
