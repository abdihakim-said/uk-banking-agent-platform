from __future__ import annotations

import json

import httpx
import pytest
from jwt import PyJWK

from bank_assistant.config import Settings
from bank_assistant.demo_idp.app import DemoIdpDisabledError, create_app
from bank_assistant.gateway.auth import JwtVerifier

ISSUER = "http://demo-idp:8000"


def settings(env: str = "dev") -> Settings:
    return Settings(environment=env, oidc_issuer=ISSUER)  # type: ignore[arg-type]


def test_refuses_to_start_unless_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BANK_DEMO_IDP_ENABLED", raising=False)
    with pytest.raises(DemoIdpDisabledError):
        create_app(settings())


def test_refuses_to_start_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BANK_DEMO_IDP_ENABLED", "true")
    prod = Settings.model_construct(environment="prod", oidc_issuer=ISSUER)
    with pytest.raises(DemoIdpDisabledError):
        create_app(prod)


async def test_tokens_validate_against_published_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BANK_DEMO_IDP_ENABLED", "true")
    app = create_app(settings())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://idp") as c:
        assert [p["given_name"] for p in (await c.get("/personas")).json()] == ["Amina", "Daniel"]
        assert (await c.post("/token", json={"customer_id": "C999"})).status_code == 404
        token = (await c.post("/token", json={"customer_id": "C002"})).json()["access_token"]
        jwk = (await c.get("/.well-known/jwks.json")).json()["keys"][0]
    pem = PyJWK.from_json(json.dumps(jwk)).key
    from cryptography.hazmat.primitives import serialization

    public_pem = pem.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    principal = JwtVerifier(ISSUER, "uk-banking-assistant", "banking.chat", public_key_pem=public_pem).verify(
        f"Bearer {token}")
    assert principal.customer_id == "C002" and principal.names == ["Daniel", "Okafor"]
