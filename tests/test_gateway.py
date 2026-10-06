from __future__ import annotations

import time

from bank_assistant.testing import Stack, build_stack
from tests.conftest import TODAY


async def post(stack: Stack, token: str | None, message: str = "balance?", **body: str) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = await stack.client.post("/v1/chat", json={"message": message, **body}, headers=headers)
    return r.status_code


async def test_rejects_missing_invalid_expired_and_misissued_tokens(stack: Stack) -> None:
    now = int(time.time())
    assert await post(stack, None) == 401
    assert await post(stack, "not-a-jwt") == 401
    assert await post(stack, stack.token(exp=now - 3600, iat=now - 7200)) == 401
    assert await post(stack, stack.token(aud="some-other-api")) == 401
    assert await post(stack, stack.token(iss="https://evil.example")) == 401
    assert await post(stack, stack.token(scope="profile")) == 403
    assert await post(stack, stack.token(customer_id=None)) == 403
    assert await post(stack, stack.token()) == 200


async def test_token_signed_by_another_key_is_rejected(stack: Stack) -> None:
    async with build_stack(today=TODAY) as other:
        assert await post(stack, other.token()) == 401


async def test_rate_limit_per_customer() -> None:
    async with build_stack(today=TODAY, settings_overrides={"rate_limit_per_minute": 2}) as s:
        token = s.token()
        codes = [await post(s, token, "hello") for _ in range(3)]
        assert codes == [200, 200, 429]
        r = await s.client.post("/v1/chat", json={"message": "hi"}, headers={"Authorization": f"Bearer {token}"})
        assert int(r.headers["Retry-After"]) > 0


async def test_oversized_and_overlong_messages(stack: Stack) -> None:
    assert await post(stack, stack.token(), "x" * 2001) == 422
    r = await stack.client.post("/v1/chat", content=b"x" * 20_000,
                                headers={"Authorization": f"Bearer {stack.token()}",
                                         "Content-Type": "application/json"})
    assert r.status_code == 413


async def test_session_cannot_be_used_by_another_customer(stack: Stack) -> None:
    first = await stack.chat("hello")
    sid = first.json()["session_id"]
    assert (await stack.chat("balance?", sid, customer_id="C002")).status_code == 403


async def test_concurrent_turn_on_same_session_is_rejected(stack: Stack) -> None:
    sid = (await stack.chat("hello")).json()["session_id"]
    await stack.redis.set(f"sess:{sid}:lock", "1")
    assert (await stack.chat("balance?", sid)).status_code == 409


async def test_security_headers(stack: Stack) -> None:
    r = await stack.chat("hello")
    assert r.headers["Cache-Control"] == "no-store"
    assert "max-age" in r.headers["Strict-Transport-Security"]
