"""Strong customer authentication for high-risk actions (card replacement, contact detail changes)."""

from __future__ import annotations

import time

import httpx
import pytest

from bank_assistant.agents.policy import PendingAction, action_binding
from bank_assistant.config import Settings
from bank_assistant.demo_idp.app import create_app as create_idp
from bank_assistant.gateway.auth import JwtVerifier, Principal
from bank_assistant.testing import Stack


async def stolen_card(stack: Stack) -> tuple[str, dict]:
    r = (await stack.chat("My card was stolen")).json()
    assert r["awaiting_confirmation"] and r["pending_action"]["sca_required"] is True
    assert "one-time code" in r["reply"]
    return r["session_id"], r["pending_action"]


def test_high_risk_tools_need_sca_and_bindings_are_exact() -> None:
    card = PendingAction("service", "reissue_card", {"card_ref": "c1", "reason": "lost"}, "s", "card_reissue")
    other = PendingAction("service", "reissue_card", {"card_ref": "c2", "reason": "lost"}, "s", "card_reissue")
    dispute = PendingAction("transactions", "raise_dispute", {"transaction_id": "t"}, "s", "dispute")
    assert card.sca_required and not dispute.sca_required
    assert card.binding != other.binding == action_binding("reissue_card", {"reason": "lost", "card_ref": "c2"})
    assert "arguments" not in card.public()


async def test_yes_alone_does_not_replace_the_card(stack: Stack) -> None:
    sid, _ = await stolen_card(stack)
    r = (await stack.chat("yes", sid)).json()
    assert "extra security" in r["reply"] and r["awaiting_confirmation"]
    assert stack.bank.cards["card_c001_1"].status == "active"


async def test_step_up_bound_to_this_action_replaces_the_card(stack: Stack) -> None:
    sid, action = await stolen_card(stack)
    await stack.chat("yes", sid)  # refused, but the action stays pending
    r = (await stack.chat("yes", sid, sca_token=stack.step_up(action["binding"]))).json()
    assert "replacement card order has been submitted" in r["reply"]
    assert stack.bank.cards["card_c001_1"].status == "replaced"


@pytest.mark.parametrize("case", ["other_action", "other_customer", "too_old", "not_sca"])
async def test_wrong_step_up_is_refused(stack: Stack, case: str) -> None:
    sid, action = await stolen_card(stack)
    now = int(time.time())
    token = {
        "other_action": stack.step_up("0" * 32),
        "other_customer": stack.step_up(action["binding"], customer_id="C002"),
        "too_old": stack.step_up(action["binding"], iat=now - 900, exp=now + 60),
        "not_sca": stack.step_up(action["binding"], acr="urn:bank:password"),
    }[case]
    resp = await stack.chat("yes", sid, sca_token=token)
    if case == "other_action":  # a valid token for a different action: the agent refuses and keeps it pending
        assert "extra security" in resp.json()["reply"]
    else:  # the gateway rejects the token itself
        assert resp.status_code in (401, 403)
    assert stack.bank.cards["card_c001_1"].status == "active"


async def test_audit_records_sca_refusal_and_success(stack: Stack) -> None:
    from bank_assistant.core.audit import AuditLog
    from bank_assistant.testing import AUDIT_KEY

    sid, action = await stolen_card(stack)
    await stack.chat("yes", sid)
    await stack.chat("yes", sid, sca_token=stack.step_up(action["binding"]))
    events = [e["event"] for e in await AuditLog(stack.redis, AUDIT_KEY, "t").tail(30)]
    assert "sca_required" in events and "sca_verified" in events and "action_executed" in events


async def test_demo_idp_one_time_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BANK_DEMO_IDP_ENABLED", "true")
    issuer = "https://demo-idp:8000"
    app = create_idp(Settings(environment="dev", oidc_issuer=issuer))  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://idp") as c:
        access = (await c.post("/token", json={"customer_id": "C001"})).json()["access_token"]
        assert (await c.post("/sca/challenge", json={"txn": "ab" * 16})).status_code == 401  # must be signed in
        ch = (await c.post("/sca/challenge", json={"txn": "ab" * 16},
                           headers={"Authorization": f"Bearer {access}"})).json()
        assert ch["delivered_to"] == "phone ending 123"
        wrong = "000000" if ch["demo_code"] != "000000" else "111111"
        assert "2 attempts left" in (await c.post("/sca/verify", json={"challenge_id": ch["challenge_id"],
                                                                          "code": wrong})).json()["detail"]
        ok = await c.post("/sca/verify", json={"challenge_id": ch["challenge_id"], "code": ch["demo_code"]})
        step_up = ok.json()["step_up_token"]
        reuse = await c.post("/sca/verify", json={"challenge_id": ch["challenge_id"], "code": ch["demo_code"]})
        assert reuse.status_code == 400  # one use only
        jwk = (await c.get("/.well-known/jwks.json")).json()["keys"][0]

        ch2 = (await c.post("/sca/challenge", json={"txn": "cd" * 16},
                            headers={"Authorization": f"Bearer {access}"})).json()
        for _ in range(3):
            last = await c.post("/sca/verify", json={"challenge_id": ch2["challenge_id"], "code": "999999"
                                                     if ch2["demo_code"] != "999999" else "888888"})
        assert "too many wrong codes" in last.json()["detail"]

    import json as _json

    from cryptography.hazmat.primitives import serialization
    from jwt import PyJWK

    pem = PyJWK.from_json(_json.dumps(jwk)).key.public_bytes(serialization.Encoding.PEM,
                                                             serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    verifier = JwtVerifier(issuer, "uk-banking-assistant", "banking.chat", public_key_pem=pem)
    principal = Principal("demo-c001", "C001", ["Amina"], frozenset({"banking.chat"}))
    assert verifier.verify_step_up(step_up, principal) == {"txn": "ab" * 16, "acr": "urn:bank:sca:otp"}
