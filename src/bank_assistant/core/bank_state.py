"""Mutable banking state shared by every replica of the MCP servers.

Reference data (customers, accounts, transactions) is seeded identically in every process. What changes at runtime
(disputes, replaced cards, cheque-book orders, service requests, reference numbers) lives here. With Redis, all
replicas see the same state, and "only once" rules are enforced atomically with SET NX, so two replicas handling the
same request at the same moment can't both accept it.
"""

from __future__ import annotations

import threading
from typing import Protocol

from redis import Redis


class BankState(Protocol):
    def claim(self, key: str) -> bool:
        """Set the key if it doesn't exist. True if this call set it: the "first and only" check."""

    def exists(self, key: str) -> bool: ...

    def members(self, prefix: str) -> set[str]:
        """Suffixes of all claimed keys starting with ``prefix`` (e.g. every disputed transaction id)."""

    def next_id(self) -> int: ...

    def save_request(self, customer_id: str, reference: str, payload: str) -> None: ...

    def requests_for(self, customer_id: str) -> list[str]: ...


class MemoryBankState:
    """Single-process state (tests, the offline demo)."""

    def __init__(self, first_id: int = 100001) -> None:
        self._lock = threading.Lock()
        self._keys: set[str] = set()
        self._next = first_id
        self._requests: dict[str, dict[str, str]] = {}

    def claim(self, key: str) -> bool:
        with self._lock:
            if key in self._keys:
                return False
            self._keys.add(key)
            return True

    def exists(self, key: str) -> bool:
        return key in self._keys

    def members(self, prefix: str) -> set[str]:
        return {k[len(prefix):] for k in self._keys if k.startswith(prefix)}

    def next_id(self) -> int:
        with self._lock:
            value, self._next = self._next, self._next + 1
            return value

    def save_request(self, customer_id: str, reference: str, payload: str) -> None:
        with self._lock:
            self._requests.setdefault(customer_id, {})[reference] = payload

    def requests_for(self, customer_id: str) -> list[str]:
        return list(self._requests.get(customer_id, {}).values())


class RedisBankState:
    """Shared state for every replica. Keys are namespaced under ``bank:``."""

    def __init__(self, redis: Redis, prefix: str = "bank:", first_id: int = 100001) -> None:
        self.redis = redis
        self.prefix = prefix
        self.redis.set(f"{prefix}refseq", first_id - 1, nx=True)  # start the counter once, whichever replica is first

    def claim(self, key: str) -> bool:
        return bool(self.redis.set(f"{self.prefix}flag:{key}", "1", nx=True))

    def exists(self, key: str) -> bool:
        return bool(self.redis.exists(f"{self.prefix}flag:{key}"))

    def members(self, prefix: str) -> set[str]:
        start = f"{self.prefix}flag:{prefix}"
        return {(k.decode() if isinstance(k, bytes) else k)[len(start):]
                for k in self.redis.scan_iter(match=f"{start}*", count=500)}

    def next_id(self) -> int:
        return int(self.redis.incr(f"{self.prefix}refseq"))  # type: ignore[arg-type]

    def save_request(self, customer_id: str, reference: str, payload: str) -> None:
        self.redis.hset(f"{self.prefix}requests:{customer_id}", reference, payload)

    def requests_for(self, customer_id: str) -> list[str]:
        raw: dict[bytes | str, bytes | str] = self.redis.hgetall(f"{self.prefix}requests:{customer_id}")  # type: ignore[assignment]
        return [v.decode() if isinstance(v, bytes) else v for v in raw.values()]
