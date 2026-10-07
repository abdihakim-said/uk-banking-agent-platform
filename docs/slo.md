# Service level objectives

| SLO | Target | Measured as | Error budget (30 days) |
|---|---|---|---|
| **Availability** | 99.5% of chat turns get a real answer | Bad: gateway 5xx from the agent (`agent_error`, `agent_unavailable`) plus turns where every model provider was down (`agent_turns_total{outcome="unavailable"}`, a 200 that says "assistant unavailable"). Total: all turns that reached the agent. Auth failures and rate limits are excluded, because they're the client's error. | 0.5%, about 3.6 hours of full outage |
| **Latency** | 95% of successful turns within 15 seconds | `gateway_request_duration_seconds` histogram, bucket `le="15.0"` | 5% of turns |

15 seconds rather than 1–2 is deliberate: a turn can include several model calls and tool calls.

## Alerts (multi-window, multi-burn-rate)

From the Google SRE workbook. Each alert needs both a long and a short window above the threshold, so it fires fast on
real incidents and clears quickly afterwards.

| Severity | Long window | Short window | Burn rate | Budget spent |
|---|---|---|---|---|
| page | 1h | 5m | 14.4x | 2% in 1 hour |
| page | 6h | 30m | 6x | 5% in 6 hours |
| ticket | 1d | 2h | 3x | 10% in 1 day |
| ticket | 3d | 6h | 1x | 10% in 3 days |

Operational alerts (`files/rules/alerts.yaml`):
- a service is down
- a fallback model provider is active
- the output guard is replacing replies
- LLM spend is above $20 an hour
- tool error rate is above 20%

## Changing them

Edit `scripts/gen_slo_rules.py` and run `make rules`. That regenerates the rule files and the dashboard, validates them
with `promtool`, and runs `deploy/prometheus/tests/slo_test.yaml`. The tests prove that:
- 9% errors pages within the hour
- 0.2% errors stays quiet
- 20% slow turns raises the latency ticket alert

## Where to see them

- **Docker Compose:** Grafana at http://localhost:3300, the "UK Banking Agent Platform" dashboard (SLO tiles, burn
  rate, latency, cost, tools, guardrails).
- **Kubernetes:** the same dashboard via the Grafana sidecar, and the same rules as a `PrometheusRule`.
