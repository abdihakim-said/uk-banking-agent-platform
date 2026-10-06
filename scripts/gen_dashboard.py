"""Generate the Grafana dashboard (deploy/helm/uk-banking-agent-platform/files/dashboards/bank-assistant.json).

    python scripts/gen_dashboard.py

Layout, top to bottom: SLO headline tiles, then SLO burn and latency, then LLM cost and tokens, then providers and
tools, then the safety signals (guardrails, PII redaction, confirmations, rate limits). Each time series panel has
one y-axis and one unit.
"""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path
from typing import Any

OUT = Path("deploy/helm/uk-banking-agent-platform/files/dashboards/bank-assistant.json")
DS = {"type": "prometheus", "uid": "${datasource}"}
_ids = count(1)


def target(expr: str, legend: str = "", ref: str = "A") -> dict[str, Any]:
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref, "range": True}


def stat(title: str, expr: str, unit: str, x: int, y: int, w: int = 4, thresholds: list[tuple[str, float | None]] | None = None,
         decimals: int | None = None, description: str = "") -> dict[str, Any]:
    steps = [{"color": c, "value": v} for c, v in (thresholds or [("text", None)])]
    return {
        "id": next(_ids), "type": "stat", "title": title, "description": description, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": 4},
        "targets": [target(expr, ref="A") | {"instant": True, "range": False}],
        "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals,
                                     "thresholds": {"mode": "absolute", "steps": steps},
                                     "color": {"mode": "thresholds"}}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "none", "textMode": "value", "justifyMode": "center"},
    }


def series(title: str, targets: list[dict[str, Any]], unit: str, x: int, y: int, w: int = 12, h: int = 8,
           description: str = "", stack: bool = False, thresholds: list[tuple[str, float | None]] | None = None,
           ) -> dict[str, Any]:
    custom: dict[str, Any] = {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 10 if stack else 0, "showPoints": "never",
                              "spanNulls": False, "axisSoftMin": 0}
    if stack:
        custom["stacking"] = {"mode": "normal", "group": "A"}
    defaults: dict[str, Any] = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic"}}
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": [{"color": c, "value": v} for c, v in thresholds]}
        custom["thresholdsStyle"] = {"mode": "dashed"}
    return {
        "id": next(_ids), "type": "timeseries", "title": title, "description": description, "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def row(title: str, y: int) -> dict[str, Any]:
    return {"id": next(_ids), "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
            "panels": []}


def build() -> dict[str, Any]:
    ok_red = [("red", None), ("green", 0.995)]
    panels = [
        row("Service level objectives", 0),
        stat("Availability (30d)", "1 - slo:availability_error_ratio:rate30d", "percentunit", 0, 1, decimals=3,
             thresholds=ok_red, description="Share of chat turns that got a real answer. SLO: 99.5%."),
        stat("Error budget left", "1 - (slo:availability_error_ratio:rate30d / 0.005)", "percentunit", 4, 1,
             decimals=1, thresholds=[("red", None), ("orange", 0.25), ("green", 0.5)],
             description="Share of the 30-day error budget (0.5%) still unspent."),
        stat("Turns within 15s (30d)", "1 - slo:latency_slow_ratio:rate30d", "percentunit", 8, 1, decimals=2,
             thresholds=[("red", None), ("green", 0.95)], description="SLO: 95% of successful turns within 15 seconds."),
        stat("p95 turn latency (1h)",
             "histogram_quantile(0.95, sum by (le) (rate(gateway_request_duration_seconds_bucket[1h])))", "s", 12, 1,
             decimals=1),
        stat("LLM cost today", "sum(increase(llm_cost_usd_total[24h]))", "currencyUSD", 16, 1, decimals=2),
        stat("Chat turns (1h)", 'sum(increase(gateway_requests_total{outcome="ok"}[1h]))', "short", 20, 1, decimals=0),
        series("Availability burn rate", [
            target("slo:availability_error_ratio:rate1h / 0.005", "1h window", "A"),
            target("slo:availability_error_ratio:rate6h / 0.005", "6h window", "B"),
        ], "x", 0, 5, thresholds=[("transparent", None), ("orange", 6), ("red", 14.4)],
            description="How fast the error budget is being spent. 1x spends it exactly over 30 days; the dashed lines "
                        "are the page thresholds (14.4x on 1h, 6x on 6h)."),
        series("Chat turn latency", [
            target("histogram_quantile(0.50, sum by (le) (rate(gateway_request_duration_seconds_bucket[5m])))", "p50", "A"),
            target("histogram_quantile(0.95, sum by (le) (rate(gateway_request_duration_seconds_bucket[5m])))", "p95", "B"),
            target("histogram_quantile(0.99, sum by (le) (rate(gateway_request_duration_seconds_bucket[5m])))", "p99", "C"),
        ], "s", 12, 5, thresholds=[("transparent", None), ("red", 15)], description="Dashed line: the 15s SLO threshold."),

        row("LLM cost and usage", 13),
        series("LLM cost per hour by model", [target("sum by (model) (increase(llm_cost_usd_total[1h]))", "{{model}}")],
               "currencyUSD", 0, 14),
        series("Tokens per minute by kind", [target("sum by (kind) (rate(llm_tokens_total[5m])) * 60", "{{kind}}")],
               "short", 12, 14, description="Input, output, cache write and cache read tokens. High cache_read is good."),
        series("LLM calls by provider and outcome",
               [target("sum by (provider, outcome) (rate(llm_calls_total[5m])) * 60", "{{provider}} · {{outcome}}")],
               "short", 0, 22, description="Calls per minute. 'unavailable' means the router moved on to the next provider."),
        series("Calls served by a fallback provider",
               [target("sum by (from_provider, to_provider) (rate(llm_fallbacks_total[5m])) * 60",
                       "{{from_provider}} → {{to_provider}}")], "short", 12, 22),

        row("Tools (MCP servers)", 30),
        series("Tool calls per minute",
               [target("sum by (tool) (rate(mcp_tool_calls_total[5m])) * 60", "{{tool}}")], "short", 0, 31),
        series("Tool error ratio", [target(
            '(sum by (server) (rate(mcp_tool_calls_total{outcome="error"}[5m]))\n'
            ' or 0 * sum by (server) (rate(mcp_tool_calls_total[5m])))\n'
            '/ clamp_min(sum by (server) (rate(mcp_tool_calls_total[5m])), 1e-9)', "{{server}}")],
            "percentunit", 12, 31, thresholds=[("transparent", None), ("red", 0.2)],
            description="Errors include customers asking for things the bank refuses (e.g. disputing a credit)."),
        series("Tool latency p95", [target(
            "histogram_quantile(0.95, sum by (le, tool) (rate(mcp_tool_latency_seconds_bucket[5m])))", "{{tool}}")],
            "s", 0, 39, w=24, h=7),

        row("Safety and customer protection", 46),
        series("Guardrail interventions", [target("sum by (guard, action) (increase(guardrail_events_total[15m]))",
                                                  "{{guard}} · {{action}}")], "short", 0, 47, w=8,
               description="Input guard (injection, regulated advice, handoff) and output guard (groundedness, advice)."),
        series("PII redacted, by type", [target("sum by (type) (increase(pii_redactions_total[15m]))", "{{type}}")],
               "short", 8, 47, w=8, description="Values replaced with placeholders before reaching a model."),
        series("Action confirmations", [target("sum by (outcome) (increase(action_confirmations_total[15m]))",
                                               "{{outcome}}")], "short", 16, 47, w=8,
               description="Executed, cancelled, abandoned or expired customer confirmations."),
        series("Rate-limited requests", [target("sum by (scope) (increase(gateway_rate_limited_total[15m]))", "{{scope}}")],
               "short", 0, 55, w=12),
        series("Gateway requests by outcome", [target("sum by (outcome) (rate(gateway_requests_total[5m])) * 60",
                                                      "{{outcome}}")], "short", 12, 55, w=12),
    ]
    return {
        "uid": "bank-assistant", "title": "UK Banking Agent Platform", "tags": ["llmops", "slo", "banking"],
        "timezone": "browser", "schemaVersion": 39, "version": 1, "refresh": "30s", "editable": True,
        "time": {"from": "now-6h", "to": "now"},
        "templating": {"list": [{"name": "datasource", "type": "datasource", "query": "prometheus", "label": "Data source",
                                 "current": {}, "hide": 0}]},
        "annotations": {"list": []},
        "panels": panels,
    }


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {OUT}")
