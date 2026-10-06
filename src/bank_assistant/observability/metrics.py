"""Prometheus metrics. Labels are bounded (no customer or session ids) to keep cardinality safe."""

from __future__ import annotations

from prometheus_client import Counter, Histogram, start_http_server

_started = False


def serve_metrics(port: int | None) -> None:
    """Expose /metrics on its own port so it never shares the public API listener (once per process)."""
    global _started
    if port and not _started:
        start_http_server(port)
        _started = True


GATEWAY_REQUESTS = Counter("gateway_requests_total", "Requests handled by the gateway", ["outcome"])
GATEWAY_LATENCY = Histogram("gateway_request_duration_seconds", "Chat latency at the gateway (successful turns)",
                            buckets=(0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120))
RATE_LIMITED = Counter("gateway_rate_limited_total", "Requests rejected by rate limiting", ["scope"])
PII_REDACTIONS = Counter("pii_redactions_total", "PII values replaced with placeholders", ["type", "stage"])

AGENT_ROUTES = Counter("agent_routes_total", "Coordinator routing decisions", ["route", "method"])
GUARDRAIL_EVENTS = Counter("guardrail_events_total", "Guardrail interventions", ["guard", "action"])
CONFIRMATIONS = Counter("action_confirmations_total", "Customer-confirmed action outcomes", ["tool", "outcome"])

LLM_CALLS = Counter("llm_calls_total", "LLM calls by provider and outcome", ["provider", "outcome"])
LLM_FALLBACKS = Counter("llm_fallbacks_total", "Calls served by a fallback provider", ["from_provider", "to_provider"])
LLM_LATENCY = Histogram("llm_latency_seconds", "LLM call latency", ["provider"],
                        buckets=(0.25, 0.5, 1, 2, 4, 8, 16, 32, 64))
LLM_TOKENS = Counter("llm_tokens_total", "Tokens by model and kind", ["model", "kind"])
LLM_COST_USD = Counter("llm_cost_usd_total", "Estimated LLM cost in USD", ["model"])
LLM_UNPRICED = Counter("llm_unpriced_calls_total", "Calls to models missing from the price table", ["model"])
BUDGET_BLOCKS = Counter("llm_budget_blocks_total", "Turns refused because a budget was spent", ["scope"])

TOOL_CALLS = Counter("mcp_tool_calls_total", "MCP tool calls", ["server", "tool", "outcome"])
TOOL_LATENCY = Histogram("mcp_tool_latency_seconds", "MCP tool latency", ["server", "tool"],
                         buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5))
AGENT_TURNS = Counter("agent_turns_total", "Agent turns by outcome (unavailable and budget_exceeded are degraded)",
                      ["outcome"])
TURN_LATENCY = Histogram("agent_turn_latency_seconds", "End-to-end agent turn latency", ["route"],
                         buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64))
