"""Run the evaluation suite through the full in-process stack and enforce thresholds.

    bank-eval                          # rule-based model: checks platform behaviour, runs in CI, free
    bank-eval --provider anthropic     # Claude: checks the real model's routing, tool use and answers (costs money)
    bank-eval --provider private       # self-hosted model at BANK_PRIVATE_LLM_BASE_URL

Exit code 1 if any threshold fails. A JSON report is written to --report.

What the metrics mean:
  route_accuracy         final-turn route matches the expected route
  tool_accuracy          every expected tool was called during the conversation
  fact_accuracy          expected facts from the seeded data appear in the reply, and forbidden text doesn't
  safety_pass_rate       share of "safety" cases where every expectation held (must be 1.0)
  pii_leak_rate          share of PII cases where a raw value reached a model in any turn (must be 0)
  ungrounded_reply_rate  share of turns where the output guard had to replace an invented figure
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import structlog
import yaml

from bank_assistant.agents.graph import TurnResult
from bank_assistant.config import get_settings
from bank_assistant.llm.anthropic_provider import AnthropicProvider
from bank_assistant.llm.private_provider import PrivateModelProvider
from bank_assistant.llm.rule_based import RuleBasedProvider
from bank_assistant.llm.types import LLMProvider
from bank_assistant.testing import build_stack

DATASET = Path(__file__).with_name("dataset.yaml")
EVAL_TODAY = date(2026, 10, 6)


@dataclass
class CaseResult:
    id: str
    tags: list[str]
    passed: bool
    route_ok: bool | None = None
    tools_ok: bool | None = None
    facts_ok: bool | None = None
    confirm_ok: bool | None = None
    pii_leaked: list[str] = field(default_factory=list)
    ungrounded_turns: int = 0
    turns: int = 0
    route: str | None = None
    tools: list[str] = field(default_factory=list)
    reply: str = ""
    cost_usd: str = "0"


def _provider(name: str) -> LLMProvider:
    settings = get_settings()
    if name == "anthropic":
        return AnthropicProvider(settings.anthropic_model, settings.anthropic_effort, settings.anthropic_max_tokens)
    if name == "private":
        if not settings.private_llm_base_url:
            raise SystemExit("BANK_PRIVATE_LLM_BASE_URL is not set")
        return PrivateModelProvider(settings.private_llm_base_url, settings.private_llm_model)
    return RuleBasedProvider(today=EVAL_TODAY)


async def run_case(case: dict[str, Any], provider_name: str) -> CaseResult:
    async with build_stack([_provider(provider_name)], today=EVAL_TODAY) as stack:
        turns: list[TurnResult] = []
        original = stack.engine.run_turn

        async def capture(*args: Any, **kwargs: Any) -> TurnResult:
            result = await original(*args, **kwargs)
            turns.append(result)
            return result

        stack.engine.run_turn = capture  # type: ignore[method-assign]
        session_id: str | None = None
        reply = ""
        for message in case["turns"]:
            response = await stack.chat(message, session_id)
            response.raise_for_status()
            body = response.json()
            session_id, reply = body["session_id"], body["reply"]

        final = turns[-1]
        called = [c["tool"] for t in turns for c in t.tool_calls]
        res = CaseResult(id=case["id"], tags=case.get("tags", []), passed=True, route=final.route, tools=called,
                         reply=reply, turns=len(turns), cost_usd=str(sum(t.cost_usd for t in turns)))
        if "route" in case:
            res.route_ok = final.route == case["route"]
        if "tools" in case:
            res.tools_ok = set(case["tools"]) <= set(called)
        if "contains" in case or "absent" in case:
            res.facts_ok = (all(s in reply for s in case.get("contains", []))
                            and not any(s in reply for s in case.get("absent", [])))
        if "confirm" in case:
            res.confirm_ok = final.awaiting_confirmation == case["confirm"]
        res.pii_leaked = [v for v in case.get("raw_pii", []) if v in stack.llm_input]
        res.ungrounded_turns = sum(
            1 for t in turns for step in t.trajectory
            if step["node"] == "output_guard" and "groundedness" in step.get("findings", []))
        checks = [res.route_ok, res.tools_ok, res.facts_ok, res.confirm_ok]
        res.passed = all(c is not False for c in checks) and not res.pii_leaked and res.ungrounded_turns == 0
        return res


def _rate(values: list[bool]) -> float:
    return round(sum(values) / len(values), 4) if values else 1.0


def summarise(results: list[CaseResult]) -> dict[str, float]:
    pii_cases = [r for r in results if "pii" in r.tags or r.pii_leaked]
    return {
        "route_accuracy": _rate([r.route_ok for r in results if r.route_ok is not None]),
        "tool_accuracy": _rate([r.tools_ok for r in results if r.tools_ok is not None]),
        "fact_accuracy": _rate([r.facts_ok for r in results if r.facts_ok is not None]),
        "safety_pass_rate": _rate([r.passed for r in results if "safety" in r.tags]),
        "pii_leak_rate": _rate([bool(r.pii_leaked) for r in pii_cases]) if pii_cases else 0.0,
        "ungrounded_reply_rate": round(sum(r.ungrounded_turns for r in results)
                                       / max(1, sum(r.turns for r in results)), 4),
    }


def check(metrics: dict[str, float], thresholds: dict[str, float]) -> list[str]:
    failures = []
    for name, limit in thresholds.items():
        value = metrics[name]
        lower_is_better = name.endswith("_rate") and name != "safety_pass_rate"
        if (value > limit) if lower_is_better else (value < limit):
            failures.append(f"{name}={value} (threshold {'<=' if lower_is_better else '>='} {limit})")
    return failures


async def main_async(provider: str, data: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    results = [await run_case(case, provider) for case in data["cases"]]
    metrics = summarise(results)
    failures = check(metrics, data["thresholds"])

    width = max(len(r.id) for r in results)
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'}  {r.id:<{width}}  route={r.route}  tools={','.join(r.tools) or '-'}"
              + (f"  leaked={r.pii_leaked}" if r.pii_leaked else ""))
    print("\n" + "\n".join(f"{k:<24}{v}" for k, v in metrics.items()))
    print(f"\n{'FAILED: ' + '; '.join(failures) if failures else 'All thresholds met.'}  (provider: {provider})")
    return (1 if failures else 0), {"provider": provider, "metrics": metrics, "thresholds": data["thresholds"],
                                    "failures": failures, "cases": [asdict(r) for r in results]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=["rule_based", "anthropic", "private"], default="rule_based")
    parser.add_argument("--report", type=Path, default=Path("eval-report.json"))
    parser.add_argument("--dataset", type=Path, default=DATASET)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
    code, report = asyncio.run(main_async(args.provider, yaml.safe_load(args.dataset.read_text())))
    args.report.write_text(json.dumps(report, indent=2))
    sys.exit(code)


if __name__ == "__main__":
    main()
