"""Fixed-window rate limits in Redis, per customer and per client IP.

This protects the LLM budget and the backends from a single noisy client. Volumetric DDoS protection belongs
in front of the gateway (a CDN/WAF such as AWS Shield + WAF, Azure Front Door or Cloudflare), not here.
"""

from __future__ import annotations

import time

from redis.asyncio import Redis


class RateLimiter:
    def __init__(self, redis: Redis, window_seconds: int = 60) -> None:
        self.redis = redis
        self.window = window_seconds

    async def hit(self, scope: str, key: str, limit: int) -> tuple[bool, int]:
        """Count one request. Returns (allowed, seconds until the window resets)."""
        now = int(time.time())
        window_start = now - now % self.window
        redis_key = f"rl:{scope}:{key}:{window_start}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(redis_key)
            pipe.expire(redis_key, self.window + 1)
            count, _ = await pipe.execute()
        return int(count) <= limit, self.window - (now - window_start)


class SessionLock:
    """One in-flight turn per session, so concurrent messages can't interleave state or confirmations."""

    def __init__(self, redis: Redis, ttl_seconds: int = 120) -> None:
        self.redis = redis
        self.ttl = ttl_seconds

    async def acquire(self, session_id: str) -> bool:
        return bool(await self.redis.set(f"sess:{session_id}:lock", "1", ex=self.ttl, nx=True))

    async def release(self, session_id: str) -> None:
        await self.redis.delete(f"sess:{session_id}:lock")
