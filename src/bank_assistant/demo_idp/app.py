"""Demo identity provider: issues short-lived tokens for the two synthetic customers.

It exists so the demo can be clicked through without a real IdP. It publishes its signing key as a JWKS, so the
gateway validates its tokens exactly as it would validate a production IdP's (``BANK_OIDC_JWKS_URL``).

It refuses to start unless ``BANK_DEMO_IDP_ENABLED=true`` and the environment isn't ``prod``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from jwt.algorithms import RSAAlgorithm
from pydantic import BaseModel

from bank_assistant.config import Settings, get_settings

PERSONAS = {
    "C001": {"given_name": "Amina", "family_name": "Yusuf", "description": "Current account and savings, one card"},
    "C002": {"given_name": "Daniel", "family_name": "Okafor", "description": "Current account, flagged as potentially "
                                                                           "vulnerable"},
}


class TokenRequest(BaseModel):
    customer_id: str


class DemoIdpDisabledError(RuntimeError):
    pass


def create_app(settings: Settings | None = None, issuer: str | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.environment == "prod" or os.environ.get("BANK_DEMO_IDP_ENABLED", "").lower() != "true":
        raise DemoIdpDisabledError("the demo IdP only runs with BANK_DEMO_IDP_ENABLED=true outside prod")
    issuer = issuer or settings.oidc_issuer
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # new key per start; tokens are disposable
    kid = uuid.uuid4().hex[:16]
    jwk: dict[str, Any] = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})

    app = FastAPI(title="Demo IdP (not for production)", docs_url=None, redoc_url=None)

    @app.get("/.well-known/jwks.json")
    async def jwks() -> dict[str, Any]:
        return {"keys": [jwk]}

    @app.get("/personas")
    async def personas() -> list[dict[str, str]]:
        return [{"customer_id": cid, **p} for cid, p in PERSONAS.items()]

    @app.post("/token")
    async def token(body: TokenRequest) -> dict[str, Any]:
        persona = PERSONAS.get(body.customer_id)
        if persona is None:
            raise HTTPException(status_code=404, detail="unknown persona")
        now = int(time.time())
        claims = {"iss": issuer, "aud": settings.oidc_audience, "sub": f"demo-{body.customer_id.lower()}",
                  "iat": now, "exp": now + 1800, "scope": settings.oidc_required_scope,
                  "customer_id": body.customer_id, "given_name": persona["given_name"],
                  "family_name": persona["family_name"]}
        return {"access_token": jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid}),
                "token_type": "Bearer", "expires_in": 1800, "persona": {"customer_id": body.customer_id, **persona}}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
