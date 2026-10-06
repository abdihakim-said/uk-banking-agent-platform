"""Redis-backed session state shared by the gateway and the agent service.

Keys (all expire after the session TTL):
  sess:{sid}:owner    customer id that owns the session (a session can't be reused by another customer)
  sess:{sid}:history  JSON list of {"role", "content"}; content is always redacted text
  sess:{sid}:vault    hash of PII placeholder -> original value (see security.redactor.PiiVault)
  sess:{sid}:pending  JSON of the action awaiting customer confirmation
"""

from __future__ import annotations

import json
from typing import Any

from redis.asyncio import Redis


class SessionOwnershipError(Exception):
    """The session id belongs to a different customer."""


class SessionStore:
    def __init__(self, redis: Redis, ttl_seconds: int = 1800, max_messages: int = 20) -> None:
        self.redis = redis
        self.ttl = ttl_seconds
        self.max_messages = max_messages

    @staticmethod
    def _key(session_id: str, part: str) -> str:
        return f"sess:{session_id}:{part}"

    async def claim(self, session_id: str, customer_id: str) -> None:
        """Bind a new session to a customer, or confirm an existing binding."""
        key = self._key(session_id, "owner")
        created = await self.redis.set(key, customer_id, ex=self.ttl, nx=True)
        if not created:
            owner = await self.redis.get(key)
            owner_str = owner.decode() if isinstance(owner, bytes) else owner
            if owner_str != customer_id:
                raise SessionOwnershipError(session_id)
            await self.redis.expire(key, self.ttl)

    async def history(self, session_id: str) -> list[dict[str, str]]:
        raw = await self.redis.get(self._key(session_id, "history"))
        return json.loads(raw) if raw else []

    async def append_history(self, session_id: str, *messages: dict[str, str]) -> None:
        history = (await self.history(session_id)) + list(messages)
        await self.redis.set(self._key(session_id, "history"), json.dumps(history[-self.max_messages:]), ex=self.ttl)

    async def load_vault(self, session_id: str) -> dict[str, str]:
        raw: dict[Any, Any] = await self.redis.hgetall(self._key(session_id, "vault"))
        return {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                for k, v in raw.items()}

    async def save_vault(self, session_id: str, mapping: dict[str, str]) -> None:
        if not mapping:
            return
        key = self._key(session_id, "vault")
        await self.redis.hset(key, mapping=dict(mapping))  # type: ignore[arg-type]
        await self.redis.expire(key, self.ttl)

    async def get_pending(self, session_id: str) -> dict[str, Any] | None:
        raw = await self.redis.get(self._key(session_id, "pending"))
        return json.loads(raw) if raw else None

    async def set_pending(self, session_id: str, action: dict[str, Any], ttl_seconds: int = 600) -> None:
        await self.redis.set(self._key(session_id, "pending"), json.dumps(action), ex=ttl_seconds)

    async def clear_pending(self, session_id: str) -> None:
        await self.redis.delete(self._key(session_id, "pending"))
