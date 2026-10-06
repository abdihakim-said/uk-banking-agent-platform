"""Ordered fallback across LLM providers with a per-provider circuit breaker.

Only infrastructure failures (``ProviderUnavailableError``) move on to the next provider. A bad request is
raised immediately, and a refusal is a normal response the agent handles. Within one turn the agent pins the
provider that answered first, because native tool-loop content can't be replayed to a different provider.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace

from bank_assistant.llm.types import (
    LLMProvider,
    LLMResponse,
    Message,
    ProviderUnavailableError,
    ToolSpec,
)
from bank_assistant.logging import get_logger
from bank_assistant.observability import metrics

log = get_logger(__name__)
tracer = trace.get_tracer("bank_assistant.llm")


class AllProvidersUnavailableError(Exception):
    pass


@dataclass
class _Breaker:
    failures: int = 0
    opened_at: float | None = None


class LLMRouter:
    def __init__(self, providers: Sequence[LLMProvider], failure_threshold: int = 3, cooldown_seconds: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self.providers = list(providers)
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown_seconds
        self.clock = clock
        self._breakers = {p.name: _Breaker() for p in self.providers}

    def is_open(self, name: str) -> bool:
        breaker = self._breakers[name]
        if breaker.opened_at is None:
            return False
        if self.clock() - breaker.opened_at >= self.cooldown:
            breaker.opened_at = None  # half-open: allow one trial call
            breaker.failures = self.failure_threshold - 1
            return False
        return True

    async def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec] | None = None,
                       json_schema: dict[str, Any] | None = None, pinned: str | None = None,
                       purpose: str = "agent") -> LLMResponse:
        candidates = [p for p in self.providers if pinned is None or p.name == pinned]
        last_error: Exception | None = None
        for provider in candidates:
            if self.is_open(provider.name):
                continue
            started = time.perf_counter()
            with tracer.start_as_current_span(f"llm.{purpose}") as span:
                span.set_attribute("gen_ai.system", provider.name)
                try:
                    response = await provider.complete(system=system, messages=messages, tools=tools,
                                                       json_schema=json_schema)
                except ProviderUnavailableError as exc:
                    self._failure(provider.name)
                    metrics.LLM_CALLS.labels(provider.name, "unavailable").inc()
                    span.set_attribute("error.type", type(exc).__name__)
                    log.warning("llm_provider_unavailable", provider=provider.name, error=str(exc))
                    last_error = exc
                    continue
                elapsed = time.perf_counter() - started
                self._breakers[provider.name] = _Breaker()
                metrics.LLM_CALLS.labels(provider.name, response.stop_reason).inc()
                metrics.LLM_LATENCY.labels(provider.name).observe(elapsed)
                span.set_attribute("gen_ai.response.model", response.model)
                span.set_attribute("gen_ai.usage.input_tokens", sum(u.input_tokens for u in response.usages))
                span.set_attribute("gen_ai.usage.output_tokens", sum(u.output_tokens for u in response.usages))
                span.set_attribute("gen_ai.response.finish_reasons", [response.stop_reason])
                if provider is not candidates[0]:
                    metrics.LLM_FALLBACKS.labels(candidates[0].name, provider.name).inc()
                return response
        raise AllProvidersUnavailableError(str(last_error) if last_error else "all provider circuits are open")

    def _failure(self, name: str) -> None:
        breaker = self._breakers[name]
        breaker.failures += 1
        if breaker.failures >= self.failure_threshold:
            breaker.opened_at = self.clock()
            log.warning("llm_circuit_opened", provider=name, cooldown_seconds=self.cooldown)
