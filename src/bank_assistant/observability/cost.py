"""Token cost accounting per call, per session and per customer per day, with budgets.

Prices are USD per million tokens from Anthropic's list prices (check them before relying on the figures;
pass your own ``PriceTable`` for negotiated rates). Self-hosted models are priced at zero per token here;
their real cost is the GPU bill, which belongs in infrastructure FinOps, not per-call accounting.

A model missing from the table is priced at the most expensive known rate and counted in
``llm_unpriced_calls_total``: over-estimating is safer than silently reporting zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from redis.asyncio import Redis

from bank_assistant.llm.types import Usage
from bank_assistant.observability import metrics

_MTOK = Decimal(1_000_000)


@dataclass(frozen=True)
class Price:
    input: Decimal
    output: Decimal
    cache_write: Decimal
    cache_read: Decimal


def _p(inp: str, out: str, read: str) -> Price:
    # 5-minute cache writes are billed at 1.25x the input price.
    return Price(Decimal(inp), Decimal(out), Decimal(inp) * Decimal("1.25"), Decimal(read))


DEFAULT_PRICES: dict[str, Price] = {
    "claude-fable-5-1": _p("10", "50", "0.25"),
    "claude-opus-5-5": _p("4", "20", "0.20"),
    "claude-opus-5": _p("5", "25", "0.50"),
    "claude-opus-4-8": _p("5", "25", "0.50"),
    "claude-sonnet-5-5": _p("2", "10", "0.20"),
    "claude-haiku-4-5": _p("1", "5", "0.10"),
}


@dataclass
class PriceTable:
    prices: dict[str, Price] = field(default_factory=lambda: dict(DEFAULT_PRICES))

    def cost(self, usage: Usage) -> Decimal:
        if usage.model.startswith("private:") or usage.model == "rule-based":
            return Decimal("0")
        price = self.prices.get(usage.model)
        if price is None:
            metrics.LLM_UNPRICED.labels(usage.model).inc()
            price = max(self.prices.values(), key=lambda p: p.output)
        return (
            Decimal(usage.input_tokens) * price.input
            + Decimal(usage.output_tokens) * price.output
            + Decimal(usage.cache_creation_input_tokens) * price.cache_write
            + Decimal(usage.cache_read_input_tokens) * price.cache_read
        ) / _MTOK


class BudgetExceededError(Exception):
    def __init__(self, scope: str) -> None:
        super().__init__(f"{scope} budget exceeded")
        self.scope = scope


class CostTracker:
    def __init__(self, redis: Redis, prices: PriceTable | None = None, session_budget: Decimal = Decimal("0.50"),
                 customer_daily_budget: Decimal = Decimal("5.00"), ttl_seconds: int = 2 * 86400) -> None:
        self.redis = redis
        self.prices = prices or PriceTable()
        self.session_budget = session_budget
        self.customer_daily_budget = customer_daily_budget
        self.ttl = ttl_seconds

    @staticmethod
    def _keys(session_id: str, customer_id: str) -> tuple[str, str]:
        day = datetime.now(UTC).strftime("%Y%m%d")
        return f"cost:sess:{session_id}", f"cost:cust:{customer_id}:{day}"

    async def spent(self, session_id: str, customer_id: str) -> tuple[Decimal, Decimal]:
        sess_key, cust_key = self._keys(session_id, customer_id)
        sess, cust = await self.redis.mget(sess_key, cust_key)
        return Decimal((sess or b"0").decode() if isinstance(sess, bytes) else (sess or "0")), \
            Decimal((cust or b"0").decode() if isinstance(cust, bytes) else (cust or "0"))

    async def check_budget(self, session_id: str, customer_id: str) -> None:
        """Called before every model call. Budgets are checked, not reserved, so one in-flight call can
        overshoot by its own cost; that bound is acceptable at these per-call sizes."""
        session_spent, customer_spent = await self.spent(session_id, customer_id)
        if session_spent >= self.session_budget:
            metrics.BUDGET_BLOCKS.labels("session").inc()
            raise BudgetExceededError("session")
        if customer_spent >= self.customer_daily_budget:
            metrics.BUDGET_BLOCKS.labels("customer_daily").inc()
            raise BudgetExceededError("customer_daily")

    async def record(self, session_id: str, customer_id: str, usages: list[Usage]) -> Decimal:
        total = Decimal("0")
        for usage in usages:
            cost = self.prices.cost(usage)
            total += cost
            metrics.LLM_COST_USD.labels(usage.model).inc(float(cost))
            for kind, count in (("input", usage.input_tokens), ("output", usage.output_tokens),
                                ("cache_write", usage.cache_creation_input_tokens),
                                ("cache_read", usage.cache_read_input_tokens)):
                if count:
                    metrics.LLM_TOKENS.labels(usage.model, kind).inc(count)
        if total:
            sess_key, cust_key = self._keys(session_id, customer_id)
            async with self.redis.pipeline(transaction=True) as pipe:
                for key in (sess_key, cust_key):
                    pipe.incrbyfloat(key, float(total))
                    pipe.expire(key, self.ttl)
                await pipe.execute()
        return total
