"""Mutual TLS between services.

Every internal hop (web → gateway → agent → MCP servers, gateway → IdP, everything → Redis) uses TLS where both sides
present a certificate signed by the internal CA. A caller without a valid client certificate can't complete the
handshake, even if a network policy lets its packets through.

Certificates come from cert-manager in Kubernetes, or from ``scripts/dev_certs.py`` locally. Every pod mounts its own
certificate at ``/certs/{tls.crt,tls.key,ca.crt}``.
"""

from __future__ import annotations

import ssl
from typing import Any

from bank_assistant.config import Settings


def client_context(settings: Settings, trust_public_cas: bool = False) -> ssl.SSLContext | None:
    """SSL context for calling another internal service: verify it against the CA and present our certificate.
    ``trust_public_cas`` also trusts the system CAs, for URLs that may be outside the cluster (a real IdP's JWKS)."""
    if not settings.tls_enabled:
        return None
    if trust_public_cas:
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ctx.load_verify_locations(cafile=settings.tls_ca_file)
    else:
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=settings.tls_ca_file)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(settings.tls_cert_file, settings.tls_key_file)
    return ctx


def server_kwargs(settings: Settings) -> dict[str, Any]:
    """uvicorn settings for a server that requires a client certificate from the internal CA."""
    if not settings.tls_enabled:
        return {}
    return {"ssl_certfile": settings.tls_cert_file, "ssl_keyfile": settings.tls_key_file,
            "ssl_ca_certs": settings.tls_ca_file, "ssl_cert_reqs": ssl.CERT_REQUIRED}
