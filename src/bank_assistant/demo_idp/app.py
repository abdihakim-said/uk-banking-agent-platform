"""Demo identity provider: issues short-lived tokens for the two synthetic customers.

It exists so the demo can be clicked through without a real IdP. It publishes its signing key as a JWKS, so the
gateway validates its tokens exactly as it would validate a production IdP's (``BANK_OIDC_JWKS_URL``).

It also stands in for the bank's strong customer authentication (SCA): for a high-risk action it "sends" a one-time
code to the customer's registered phone and exchanges a correct code for a short-lived step-up token bound to that
action. In a real bank this is the identity provider's step-up flow (``acr_values``) or an in-app push approval. Here
the code is also returned in the response as ``demo_code``, so the demo can show the "phone".

It refuses to start unless ``BANK_DEMO_IDP_ENABLED=true`` and the environment isn't ``prod``.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
import uuid
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Header, HTTPException
from jwt.algorithms import RSAAlgorithm
from pydantic import BaseModel, Field

from bank_assistant.config import Settings, get_settings
from bank_assistant.gateway.auth import SCA_ACR, STEP_UP_MAX_AGE_SECONDS

PERSONAS = {
    "C001": {"given_name": "Amina", "family_name": "Yusuf", "description": "Current account and savings, one card",
             "phone_hint": "phone ending 123"},
    "C002": {"given_name": "Daniel", "family_name": "Okafor",
             "description": "Current account, flagged as potentially vulnerable", "phone_hint": "phone ending 456"},
}


class TokenRequest(BaseModel):
    customer_id: str


class ChallengeRequest(BaseModel):
    txn: str = Field(min_length=8, max_length=64, pattern=r"^[a-f0-9]+$")  # the action binding to approve


class VerifyRequest(BaseModel):
    challenge_id: str
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


CODE_TTL_SECONDS = 300
MAX_ATTEMPTS = 3


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
    challenges: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()

    def sign(claims: dict[str, Any]) -> str:
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})

    def customer_from_bearer(authorization: str | None) -> dict[str, Any]:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="sign in first")
        try:
            return jwt.decode(authorization.split(" ", 1)[1], key.public_key(), algorithms=["RS256"],
                              audience=settings.oidc_audience, issuer=issuer)
        except jwt.PyJWTError as exc:
            raise HTTPException(status_code=401, detail="invalid session") from exc

    @app.post("/sca/challenge")
    async def sca_challenge(body: ChallengeRequest,
                            authorization: str | None = Header(default=None)) -> dict[str, Any]:
        """Start strong customer authentication for one action: send a one-time code to the registered phone."""
        claims = customer_from_bearer(authorization)
        persona = PERSONAS[claims["customer_id"]]
        challenge_id = uuid.uuid4().hex
        code = f"{secrets.randbelow(1_000_000):06d}"
        with lock:
            now = time.time()
            for cid in [c for c, v in challenges.items() if v["expires"] < now]:
                del challenges[cid]
            challenges[challenge_id] = {"sub": claims["sub"], "customer_id": claims["customer_id"], "txn": body.txn,
                                        "code": code, "expires": now + CODE_TTL_SECONDS, "attempts": 0}
        return {"challenge_id": challenge_id, "delivered_to": persona["phone_hint"], "expires_in": CODE_TTL_SECONDS,
                "demo_code": code}  # demo only: a real bank sends this by SMS or push and never returns it

    @app.post("/sca/verify")
    async def sca_verify(body: VerifyRequest) -> dict[str, Any]:
        """Exchange a correct code for a step-up token bound to the approved action. Three wrong codes end it."""
        with lock:
            ch = challenges.get(body.challenge_id)
            if ch is None or ch["expires"] < time.time():
                challenges.pop(body.challenge_id, None)
                raise HTTPException(status_code=400, detail="this code has expired; start again")
            if not hmac.compare_digest(ch["code"], body.code):
                ch["attempts"] += 1
                left = MAX_ATTEMPTS - ch["attempts"]
                if left <= 0:
                    del challenges[body.challenge_id]
                    raise HTTPException(status_code=400, detail="too many wrong codes; start again")
                raise HTTPException(status_code=400, detail=f"that code isn't right; {left} attempts left")
            del challenges[body.challenge_id]  # one use only
        now = int(time.time())
        token = sign({"iss": issuer, "aud": settings.oidc_audience, "sub": ch["sub"], "customer_id": ch["customer_id"],
                      "iat": now, "auth_time": now, "exp": now + STEP_UP_MAX_AGE_SECONDS, "acr": SCA_ACR,
                      "amr": ["pwd", "otp"], "txn": ch["txn"]})
        return {"step_up_token": token, "token_type": "Bearer", "expires_in": STEP_UP_MAX_AGE_SECONDS}

    @app.get("/.well-known/jwks.json")
    async def jwks() -> dict[str, Any]:
        return {"keys": [jwk]}

    @app.get("/personas")
    async def personas() -> list[dict[str, str]]:
        return [{"customer_id": cid, **{k: v for k, v in p.items() if k != "phone_hint"}}
                for cid, p in PERSONAS.items()]

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
        return {"access_token": sign(claims),
                "token_type": "Bearer", "expires_in": 1800, "persona": {"customer_id": body.customer_id, **persona}}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
