"""The internal ops port: /metrics, /healthz and /readyz over plain HTTP on BANK_METRICS_PORT.

The API ports require a client certificate, which Kubernetes probes and Prometheus don't present, so probes and
scraping use this port instead. It serves no customer data, and NetworkPolicy limits who can reach it. /readyz runs a
real check: Redis answers, and the next service in the chain completes an mTLS handshake.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

Readiness = Callable[[], dict[str, str]]
_started = False


def serve_ops(port: int | None, readiness: Readiness | None = None) -> None:
    global _started
    if not port or _started:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/metrics":
                self._send(200, generate_latest(), CONTENT_TYPE_LATEST)
            elif self.path == "/healthz":
                self._send(200, b'{"status":"ok"}', "application/json")
            elif self.path == "/readyz":
                checks = readiness() if readiness else {}
                ready = all(v == "ok" for v in checks.values())
                body = json.dumps({"ready": ready, "checks": checks}).encode()
                self._send(200 if ready else 503, body, "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:  # probes every few seconds; keep logs clean
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - ops port inside the pod network
    threading.Thread(target=server.serve_forever, daemon=True, name="ops-server").start()
    _started = True


def http_check(url: str, ctx: object | None, timeout: float = 2.0) -> str:
    """GET a dependency's /healthz with our client certificate: proves the mTLS path works, not just the socket."""
    import ssl
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout,  # noqa: S310 - internal URLs from settings
                                    context=ctx if isinstance(ctx, ssl.SSLContext) else None) as r:
            return "ok" if r.status == 200 else f"HTTP {r.status}"
    except Exception as exc:
        return type(exc).__name__


def redis_check(client: object) -> str:
    try:
        client.ping()  # type: ignore[attr-defined]
        return "ok"
    except Exception as exc:
        return type(exc).__name__
