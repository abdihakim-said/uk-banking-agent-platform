"""The turn graph (LangGraph).

    load → input_guard ─(blocked)──────────────────────────────────────────┐
                │                                                         │
             confirm ─(pending action resolved)───────────────────────────┤
                │                                                         │
           coordinator ─┬─ accounts_agent ─────┐                          │
                        ├─ transactions_agent ─┼── output_guard ── finalise
                        ├─ service_agent ──────┘                          │
                        └─ fixed_reply (general / out of scope) ──────────┘

Every node appends a trajectory step (node, decision, latency), which is returned with the turn, logged and
attached to the trace. Message text is never put in the trajectory.
"""

from __future__ import annotations

import json
import operator
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from mcp.server.mcpserver import MCPServer
from opentelemetry import trace

from bank_assistant.agents import guards
from bank_assistant.agents.domain import run_domain_agent
from bank_assistant.agents.policy import PendingAction, classify_reply, result_message
from bank_assistant.agents.prompts import COORDINATOR, COORDINATOR_SCHEMA, REPLIES, domain_prompt
from bank_assistant.agents.toolbox import McpToolbox
from bank_assistant.core.context_token import issue_context_token
from bank_assistant.core.session_store import SessionStore
from bank_assistant.llm.router import AllProvidersUnavailableError, LLMRouter
from bank_assistant.llm.rule_based import keyword_route
from bank_assistant.llm.types import AssistantMessage, Message, ProviderError, Usage, UserMessage
from bank_assistant.logging import get_logger
from bank_assistant.observability import metrics
from bank_assistant.observability.cost import BudgetExceededError, CostTracker
from bank_assistant.security.input_guard import Verdict, check_input
from bank_assistant.security.redactor import PiiVault, Redactor

log = get_logger(__name__)
tracer = trace.get_tracer("bank_assistant.agents")
DOMAINS = ("accounts", "transactions", "service")


@dataclass
class AgentDeps:
    router: LLMRouter
    store: SessionStore
    costs: CostTracker
    redactor: Redactor
    mcp_targets: dict[str, str | MCPServer]
    context_secret: str
    context_ttl_seconds: int = 120
    today: Callable[[], date] = field(default=lambda: datetime.now(UTC).date())


class TurnState(TypedDict, total=False):
    session_id: str
    customer_id: str
    user_text: str  # already redacted by the gateway
    known_names: list[str]
    history: list[dict[str, str]]
    vault: PiiVault
    pending: dict[str, Any] | None
    route: str
    route_method: str
    reply: str
    outcome: str
    handoff: bool
    tool_payloads: list[Any]
    tool_calls: Annotated[list[dict[str, Any]], operator.add]
    provider: str | None
    cost_usd: Decimal
    awaiting_confirmation: bool
    guard_findings: list[str]
    trajectory: Annotated[list[dict[str, Any]], operator.add]


@dataclass
class TurnResult:
    reply: str
    route: str
    outcome: str
    handoff: bool
    awaiting_confirmation: bool
    provider: str | None
    cost_usd: Decimal
    trajectory: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]


def _step(node: str, started: float, **info: Any) -> list[dict[str, Any]]:
    return [{"node": node, "ms": round((time.perf_counter() - started) * 1000, 1), **info}]


def _history_messages(history: list[dict[str, str]]) -> list[Message]:
    out: list[Message] = []
    for h in history:
        out.append(UserMessage(h["content"]) if h["role"] == "user" else AssistantMessage(h["content"]))
    return out


def build_graph(deps: AgentDeps) -> Any:
    cost_box: dict[str, Decimal] = {}

    def _budget_hooks(state: TurnState) -> tuple[Callable[[], Awaitable[None]],
                                                 Callable[[list[Usage]], Awaitable[None]]]:
        sid, cid = state["session_id"], state["customer_id"]

        async def before() -> None:
            await deps.costs.check_budget(sid, cid)

        async def after(usages: list[Usage]) -> None:
            cost_box[sid] = cost_box.get(sid, Decimal("0")) + await deps.costs.record(sid, cid, usages)

        return before, after

    async def load(state: TurnState) -> dict[str, Any]:
        t = time.perf_counter()
        sid = state["session_id"]
        cost_box[sid] = Decimal("0")
        return {
            "history": await deps.store.history(sid),
            "vault": PiiVault(await deps.store.load_vault(sid)),
            "pending": await deps.store.get_pending(sid),
            "handoff": False, "awaiting_confirmation": False, "tool_payloads": [], "guard_findings": [],
            "trajectory": _step("load", t),
        }

    async def input_guard(state: TurnState) -> dict[str, Any]:
        t = time.perf_counter()
        verdict = check_input(state["user_text"])
        if verdict.verdict is Verdict.ALLOW:
            return {"trajectory": _step("input_guard", t, verdict="allow")}
        metrics.GUARDRAIL_EVENTS.labels("input", verdict.verdict.value).inc()
        reply_key = {Verdict.BLOCK_INJECTION: "injection", Verdict.REGULATED_ADVICE: "regulated_advice",
                     Verdict.HUMAN_HANDOFF: "human_handoff"}[verdict.verdict]
        return {"reply": REPLIES[reply_key], "route": reply_key, "route_method": "input_guard",
                "outcome": verdict.verdict.value, "handoff": verdict.verdict is Verdict.HUMAN_HANDOFF,
                "trajectory": _step("input_guard", t, verdict=verdict.verdict.value)}

    async def confirm(state: TurnState) -> dict[str, Any]:
        t = time.perf_counter()
        raw = state.get("pending")
        if not raw:
            return {"trajectory": _step("confirm", t, pending=False)}
        pending = PendingAction.from_dict(raw)
        answer = classify_reply(state["user_text"])
        await deps.store.clear_pending(state["session_id"])
        if pending.expired():
            metrics.CONFIRMATIONS.labels(pending.tool, "expired").inc()
            if answer == "yes":
                return {"reply": REPLIES["expired"], "route": pending.domain, "route_method": "confirmation",
                        "outcome": "expired", "trajectory": _step("confirm", t, decision="expired")}
            return {"trajectory": _step("confirm", t, decision="expired_ignored")}
        if answer == "no":
            metrics.CONFIRMATIONS.labels(pending.tool, "cancelled").inc()
            return {"reply": REPLIES["cancelled"], "route": pending.domain, "route_method": "confirmation",
                    "outcome": "cancelled", "trajectory": _step("confirm", t, decision="cancelled")}
        if answer == "other":
            metrics.CONFIRMATIONS.labels(pending.tool, "abandoned").inc()
            return {"trajectory": _step("confirm", t, decision="abandoned")}

        token = issue_context_token(state["customer_id"], state["session_id"], deps.context_secret,
                                    deps.context_ttl_seconds)
        async with McpToolbox(pending.domain, deps.mcp_targets[pending.domain]) as toolbox:
            outcome = await toolbox.call(pending.tool, pending.arguments, token, dry_run=False)
        metrics.CONFIRMATIONS.labels(pending.tool, "error" if outcome.is_error else "executed").inc()
        reply = (f"Sorry, I couldn't complete that: {outcome.text}" if outcome.is_error or outcome.data is None
                 else result_message(outcome.data))
        return {"reply": reply, "route": pending.domain, "route_method": "confirmation",
                "outcome": "action_error" if outcome.is_error else "action_executed",
                "tool_payloads": [outcome.data] if outcome.data else [],
                "tool_calls": [{"tool": pending.tool, "write": True, "dry_run": False, "is_error": outcome.is_error}],
                "trajectory": _step("confirm", t, decision="executed", tool=pending.tool)}

    async def coordinator(state: TurnState) -> dict[str, Any]:
        t = time.perf_counter()
        before, after = _budget_hooks(state)
        method = "llm"
        try:
            await before()
            response = await deps.router.complete(
                system=COORDINATOR, json_schema=COORDINATOR_SCHEMA, purpose="coordinator",
                messages=[*_history_messages(state["history"][-4:]), UserMessage(state["user_text"])])
            await after(response.usages)
            route = json.loads(response.text)["route"] if response.stop_reason != "refusal" else "out_of_scope"
            if response.provider == "rule_based":
                method = "rules"
        except BudgetExceededError:
            raise
        except (AllProvidersUnavailableError, ProviderError, ValueError, KeyError, TypeError) as exc:
            route, method = keyword_route(state["user_text"]), "fallback_rules"
            log.warning("coordinator_fallback", error=type(exc).__name__)
        if route not in (*DOMAINS, "general", "out_of_scope"):
            route = "out_of_scope"
        metrics.AGENT_ROUTES.labels(route, method).inc()
        return {"route": route, "route_method": method, "trajectory": _step("coordinator", t, route=route,
                                                                           method=method)}

    def domain_node(domain: str) -> Callable[[TurnState], Awaitable[dict[str, Any]]]:
        async def node(state: TurnState) -> dict[str, Any]:
            t = time.perf_counter()
            before, after = _budget_hooks(state)
            token = issue_context_token(state["customer_id"], state["session_id"], deps.context_secret,
                                        deps.context_ttl_seconds)
            async with McpToolbox(domain, deps.mcp_targets[domain]) as toolbox:
                result = await run_domain_agent(
                    domain=domain, system=domain_prompt(domain, deps.today()),
                    messages=[*_history_messages(state["history"]), UserMessage(state["user_text"])],
                    toolbox=toolbox, router=deps.router, context_token=token, vault=state["vault"],
                    redactor=deps.redactor, known_names=state.get("known_names", []),
                    before_model_call=before, after_model_call=after)
            if result.pending is not None:
                await deps.store.set_pending(state["session_id"], result.pending.to_dict())
            return {"reply": result.reply, "outcome": result.outcome, "provider": result.provider,
                    "tool_payloads": result.tool_payloads, "tool_calls": result.tool_calls,
                    "awaiting_confirmation": result.pending is not None,
                    "trajectory": _step(f"{domain}_agent", t, outcome=result.outcome, provider=result.provider,
                                        tools=[c["tool"] for c in result.tool_calls])}
        return node

    async def fixed_reply(state: TurnState) -> dict[str, Any]:
        t = time.perf_counter()
        return {"reply": REPLIES[state["route"]], "outcome": state["route"],
                "trajectory": _step("fixed_reply", t)}

    async def output_guard(state: TurnState) -> dict[str, Any]:
        t = time.perf_counter()
        if state.get("route_method") in ("input_guard", "confirmation") or state.get("awaiting_confirmation"):
            return {"trajectory": _step("output_guard", t, skipped=True)}
        payloads = state.get("tool_payloads", [])
        findings = guards.check_reply(state.get("reply", ""), payloads, state["user_text"])
        if not findings:
            return {"trajectory": _step("output_guard", t, findings=[])}
        for f in findings:
            metrics.GUARDRAIL_EVENTS.labels(f.guard, "replaced").inc()
        log.warning("output_guard_replaced_reply", guards=[f.guard for f in findings])
        key = "ungrounded" if any(f.guard == "groundedness" for f in findings) else "regulated_advice"
        return {"reply": REPLIES[key], "guard_findings": [f.guard for f in findings],
                "trajectory": _step("output_guard", t, findings=[f.guard for f in findings])}

    async def finalise(state: TurnState) -> dict[str, Any]:
        t = time.perf_counter()
        sid, vault = state["session_id"], state["vault"]
        # Anything identifying in the reply (from a tool preview, or echoed by the model) becomes a placeholder.
        reply = deps.redactor.redact(state.get("reply", REPLIES["step_limit"]), vault, state.get("known_names", []))
        for placeholder in vault.new_entries:
            metrics.PII_REDACTIONS.labels(placeholder[1:].rsplit("_", 1)[0], "agent").inc()
        await deps.store.save_vault(sid, vault.new_entries)
        await deps.store.append_history(sid, {"role": "user", "content": state["user_text"]},
                                        {"role": "assistant", "content": reply})
        return {"reply": reply, "cost_usd": cost_box.pop(sid, Decimal("0")), "trajectory": _step("finalise", t)}

    g: StateGraph[TurnState] = StateGraph(TurnState)
    g.add_node("load", load)
    g.add_node("input_guard", input_guard)
    g.add_node("confirm", confirm)
    g.add_node("coordinator", coordinator)
    for d in DOMAINS:
        g.add_node(f"{d}_agent", domain_node(d))  # type: ignore[arg-type]
    g.add_node("fixed_reply", fixed_reply)
    g.add_node("output_guard", output_guard)
    g.add_node("finalise", finalise)

    g.add_edge(START, "load")
    g.add_edge("load", "input_guard")
    g.add_conditional_edges("input_guard", lambda s: "blocked" if s.get("reply") else "ok",
                            {"blocked": "finalise", "ok": "confirm"})
    g.add_conditional_edges("confirm", lambda s: "done" if s.get("reply") else "route",
                            {"done": "finalise", "route": "coordinator"})
    g.add_conditional_edges("coordinator", lambda s: f"{s['route']}_agent" if s["route"] in DOMAINS else "fixed",
                            {**{f"{d}_agent": f"{d}_agent" for d in DOMAINS}, "fixed": "fixed_reply"})
    for d in DOMAINS:
        g.add_edge(f"{d}_agent", "output_guard")
    g.add_edge("fixed_reply", "finalise")
    g.add_edge("output_guard", "finalise")
    g.add_edge("finalise", END)
    return g.compile()


class AgentEngine:
    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps
        self.graph = build_graph(deps)

    async def run_turn(self, session_id: str, customer_id: str, user_text: str,
                       known_names: list[str] | None = None) -> TurnResult:
        started = time.perf_counter()
        with tracer.start_as_current_span("agent.turn") as span:
            span.set_attribute("session.id", session_id)
            try:
                state: TurnState = await self.graph.ainvoke({
                    "session_id": session_id, "customer_id": customer_id, "user_text": user_text,
                    "known_names": known_names or [], "trajectory": [], "tool_calls": [],
                })
            except BudgetExceededError as exc:
                log.warning("turn_budget_exceeded", scope=exc.scope)
                return TurnResult(REPLIES["budget"], "none", "budget_exceeded", False, False, None, Decimal("0"),
                                  [{"node": "budget", "scope": exc.scope}], [])
            except AllProvidersUnavailableError:
                log.error("turn_all_providers_unavailable")
                return TurnResult(REPLIES["unavailable"], "none", "unavailable", False, False, None, Decimal("0"),
                                  [{"node": "router", "error": "all_providers_unavailable"}], [])
            route = state.get("route", "none")
            span.set_attribute("agent.route", route)
            span.set_attribute("agent.outcome", state.get("outcome", ""))
            span.set_attribute("llm.cost_usd", float(state.get("cost_usd", 0)))
        metrics.TURN_LATENCY.labels(route).observe(time.perf_counter() - started)
        result = TurnResult(
            reply=state["reply"], route=route, outcome=state.get("outcome", "answered"),
            handoff=state.get("handoff", False), awaiting_confirmation=state.get("awaiting_confirmation", False),
            provider=state.get("provider"), cost_usd=state.get("cost_usd", Decimal("0")),
            trajectory=state.get("trajectory", []), tool_calls=state.get("tool_calls", []),
        )
        log.info("agent_turn", session_id=session_id, route=result.route, outcome=result.outcome,
                 provider=result.provider, cost_usd=str(result.cost_usd), steps=[s["node"] for s in result.trajectory],
                 tools=[c["tool"] for c in result.tool_calls])
        return result
