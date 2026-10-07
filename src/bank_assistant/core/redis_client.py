"""Redis clients with authentication and (m)TLS from settings, so no component builds its own connection."""

from __future__ import annotations

import ssl
from typing import Any

import redis
import redis.asyncio

from bank_assistant.config import Settings


def redis_kwargs(settings: Settings) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"socket_connect_timeout": 2, "socket_timeout": 5}
    if settings.redis_password is not None:
        kwargs["password"] = settings.redis_password.get_secret_value()
    if settings.redis_url.startswith("rediss://"):
        ca = settings.redis_tls_ca_file or settings.tls_ca_file
        kwargs |= {"ssl_cert_reqs": ssl.CERT_REQUIRED, "ssl_ca_certs": ca}
        if settings.tls_enabled:  # present our client certificate too (Redis runs with tls-auth-clients yes)
            kwargs |= {"ssl_certfile": settings.tls_cert_file, "ssl_keyfile": settings.tls_key_file}
    return kwargs


def async_redis(settings: Settings) -> redis.asyncio.Redis:
    return redis.asyncio.Redis.from_url(settings.redis_url, **redis_kwargs(settings))


def sync_redis(settings: Settings) -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url, **redis_kwargs(settings))
