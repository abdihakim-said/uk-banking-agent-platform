"""Tamper-evident audit log.

Every security-relevant event (sign-in failures, refused sessions, actions proposed, confirmed, cancelled or executed,
guardrail blocks, handoffs, spent budgets) is appended to one Redis stream. Each entry carries the HMAC of the previous
entry, signed with ``BANK_AUDIT_HMAC_KEY``. Editing, deleting, inserting or reordering an entry breaks the chain, and
without the key nobody can rebuild a valid one. ``bank-audit verify`` checks the whole chain.

Entries record who, what, when and the outcome: ids, tool names, references, kinds of data. Never message text or
personal data values. Appends from many replicas are serialised with an optimistic Redis transaction (WATCH), so the
chain stays linear.

In production, also ship the stream (or the ``audit`` log lines) to write-once storage, such as S3 Object Lock or
Azure immutable blobs, for the retention period the regulator requires.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import WatchError

from bank_assistant.logging import get_logger

log = get_logger("audit")
STREAM = "audit:events"
HEAD = "audit:head"
GENESIS = "genesis"


@dataclass(frozen=True)
class VerifyResult:
    entries: int
    ok: bool
    problem: str | None = None
    entry_id: str | None = None


class AuditLog:
    def __init__(self, redis: Redis, hmac_key: str, service: str) -> None:
        self.redis = redis
        self.key = hmac_key.encode()
        self.service = service

    def _mac(self, body: str) -> str:
        return hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()

    async def record(self, event: str, *, outcome: str, actor: str | None = None, session_id: str | None = None,
                     **details: Any) -> str:
        """Append one event and return its MAC. Never raises into the request path: a failure is logged loudly."""
        for _ in range(20):
            try:
                async with self.redis.pipeline(transaction=True) as pipe:
                    await pipe.watch(HEAD)
                    prev_raw = await pipe.get(HEAD)
                    prev = prev_raw.decode() if isinstance(prev_raw, bytes) else (prev_raw or GENESIS)
                    body = json.dumps({
                        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "service": self.service,
                        "event": event, "outcome": outcome, "actor": actor, "session": session_id,
                        "details": details, "prev": prev,
                    }, sort_keys=True, separators=(",", ":"))
                    mac = self._mac(body)
                    pipe.multi()  # type: ignore[no-untyped-call]
                    pipe.xadd(STREAM, {"body": body, "mac": mac})
                    pipe.set(HEAD, mac)
                    await pipe.execute()
                log.info("audit", audit_event=event, outcome=outcome, actor=actor, session_id=session_id, mac=mac[:16])
                return mac
            except WatchError:
                continue  # another replica appended first; re-read the head and try again
            except Exception as exc:
                log.error("audit_write_failed", audit_event=event, error=type(exc).__name__)
                return ""
        log.error("audit_write_contended", audit_event=event)
        return ""

    async def verify(self) -> VerifyResult:
        expected_prev = GENESIS
        count = 0
        entries = list(await self.redis.xrange(STREAM) or [])
        for entry_id, raw_fields in entries:
            fields: dict[Any, Any] = dict(raw_fields or {})
            eid = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
            body = _s(fields.get(b"body", fields.get("body")))
            mac = _s(fields.get(b"mac", fields.get("mac")))
            if not hmac.compare_digest(self._mac(body), mac):
                return VerifyResult(count, False, "entry was modified (signature doesn't match)", eid)
            if json.loads(body)["prev"] != expected_prev:
                return VerifyResult(count, False, "chain broken: an earlier entry was removed or reordered", eid)
            expected_prev = mac
            count += 1
        head = _s(await self.redis.get(HEAD)) or GENESIS
        if head != expected_prev:
            return VerifyResult(count, False, "entries were removed from the end of the log")
        return VerifyResult(count, True)

    async def tail(self, n: int = 20) -> list[dict[str, Any]]:
        entries = list(await self.redis.xrevrange(STREAM, count=n) or [])
        return [json.loads(_s((f or {}).get(b"body", (f or {}).get("body")))) for _, f in reversed(entries)]


def _s(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else (value or "")


def main() -> None:
    """``bank-audit verify`` checks the chain; ``bank-audit tail`` prints recent events."""
    from bank_assistant.config import get_settings
    from bank_assistant.core.redis_client import async_redis

    parser = argparse.ArgumentParser(description="Inspect the tamper-evident audit log")
    parser.add_argument("command", choices=["verify", "tail"])
    parser.add_argument("-n", type=int, default=20)
    args = parser.parse_args()
    settings = get_settings()
    audit = AuditLog(async_redis(settings), settings.audit_hmac_key.get_secret_value(), "cli")

    async def run() -> int:
        if args.command == "tail":
            for e in await audit.tail(args.n):
                print(f'{e["ts"]}  {e["service"]:<8} {e["event"]:<22} {e["outcome"]:<12} '
                      f'{e["actor"] or "-":<6} {json.dumps(e["details"])}')
            return 0
        result = await audit.verify()
        if result.ok:
            print(f"OK: {result.entries} entries, chain intact")
            return 0
        print(f"TAMPERED after {result.entries} valid entries: {result.problem}"
              + (f" (entry {result.entry_id})" if result.entry_id else ""))
        return 1

    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
