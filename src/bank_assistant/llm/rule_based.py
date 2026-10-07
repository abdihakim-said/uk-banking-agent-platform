"""A deterministic stand-in for an LLM, used offline (no API key), in tests and in CI evals.

It speaks the same provider interface: it classifies intent for the coordinator, picks tools from simple
keyword rules, chains two-step flows (find a transaction, then dispute it), and phrases answers from tool
results only. It is not a substitute for a real model's language understanding; it exists so the whole
platform (redaction, MCP, confirmations, guards, cost, traces) can run and be tested without one.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from bank_assistant.core.bank_data import _MERCHANTS
from bank_assistant.llm.types import (
    AssistantMessage,
    LLMResponse,
    Message,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    Usage,
    UserMessage,
)

_MERCHANT_NAMES = [m[0] for m in _MERCHANTS]

ROUTE_KEYWORDS: list[tuple[str, str]] = [
    ("transactions", r"\bdispute|recogni[sz]e|\bspen[dt]\b|\bspending\b"),
    ("service", r"\bcard\b|\bcheque|\bchequebook|\baddress\b|\bemail\b|\bphone (?:number)?\b|\bkyc\b|"
                r"\bcontact details\b|\bservice request"),
    ("transactions", r"\bdispute|recogni[sz]e|\btransactions?\b|\bpayments?\b|\bspen[dt]|\bspending\b|"
                     r"\bcharged?\b|\bdebit|\bpaid\b|\bdirect debit|" + "|".join(re.escape(m.lower())
                                                                              for m in _MERCHANT_NAMES)),
    ("accounts", r"\bbalance\b|\baccounts?\b|\bhow much (?:money|do i have)|\bavailable\b|\boverdraft\b|"
                 r"\bstatement\b|\bprofile\b"),
    ("general", r"^\s*(?:hi|hello|hey|good (?:morning|afternoon|evening)|thanks?|thank you)\b|"
                r"what can you (?:do|help)|\bhelp\b"),
]


def keyword_route(text: str) -> str:
    lowered = text.lower()
    for route, pattern in ROUTE_KEYWORDS:
        if re.search(pattern, lowered):
            return route
    return "out_of_scope"


def _merchant(text: str) -> str | None:
    return next((m for m in _MERCHANT_NAMES
                 if re.search(rf"\b{re.escape(m.lower().split()[0])}\b", text.lower())), None)


def _last_month(today: date) -> tuple[date, date]:
    end = today.replace(day=1) - timedelta(days=1)
    return end.replace(day=1), end


class RuleBasedProvider:
    name = "rule_based"

    def __init__(self, today: date | None = None) -> None:
        self.today = today or datetime.now(UTC).date()

    async def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec] | None = None,
                       json_schema: dict[str, Any] | None = None, max_tokens: int | None = None) -> LLMResponse:
        user_text = next((m.text for m in reversed(messages) if isinstance(m, UserMessage)), "")
        if json_schema is not None:
            route = keyword_route(user_text)
            return self._reply(json.dumps({"route": route, "reason": "keyword rules"}))

        tool_names = {t.name for t in tools or []}
        last = messages[-1] if messages else None
        if isinstance(last, ToolResultMessage):
            names = {c.id: c.name for m in messages if isinstance(m, AssistantMessage) for c in m.tool_calls}
            result = last.results[0]
            tool = names.get(result.call_id, "")
            if result.is_error:
                return self._reply(f"Sorry, I couldn't complete that: {result.content}")
            payload = json.loads(result.content)
            follow_up = self._follow_up(user_text, tool, payload, tool_names)
            if follow_up is not None:
                return self._call(*follow_up)
            return self._reply(self._phrase(tool, payload))

        call = self._first_call(user_text, tool_names)
        if call is None:
            return self._reply("I can help with balances, statements, transactions, spending, disputes, cards, "
                               "cheque books and contact details. What would you like to do?")
        return self._call(*call)

    # ---- decisions -------------------------------------------------------------------------
    def _first_call(self, text: str, tools: set[str]) -> tuple[str, dict[str, Any]] | None:
        t = text.lower()
        merchant = _merchant(text)
        explicit_tx = re.search(r"\btx_[a-z0-9]+_\d+\b", t)
        candidates: list[tuple[bool, str, dict[str, Any]]] = [
            # An explicit transaction id goes straight to the dispute tool, so the server's ownership check decides.
            (bool(explicit_tx) and ("dispute" in t or "recogni" in t), "raise_dispute",
             {"transaction_id": explicit_tx.group(0) if explicit_tx else None, "reason": "not_recognised"}),
            ("dispute" in t or "recogni" in t, "search_transactions",
             {"merchant": merchant, "direction": "debit", "limit": 5}),
            ("statement" in t, "request_statement", {
                "start_date": _last_month(self.today)[0].isoformat(),
                "end_date": _last_month(self.today)[1].isoformat(), "delivery": "secure_message"}),
            (bool(re.search(r"spen[dt]|spending", t)), "spending_summary", {
                "month": (_last_month(self.today)[0] if "last month" in t else self.today).strftime("%Y-%m")}),
            (bool(re.search(r"\bcard\b", t) and re.search(r"lost|stolen|damaged|replace|new card|broken|arrived",
                                                          t)), "list_cards", {}),
            ("cheque" in t, "request_cheque_book", {}),
            (bool(re.search(r"\b(?:change|update|new)\b", t) and re.search(r"address|email|phone", t)),
             "submit_kyc_update", self._kyc_args(text)),
            ("status" in t and "dispute" in t, "get_dispute_status", {}),
            ("status" in t, "get_service_request_status", {}),
            (bool(re.search(r"balance|how much|available|overdraft", t)), "get_balance", {}),
            (bool(re.search(r"\baccounts\b", t)), "list_accounts", {}),
            (bool(re.search(r"transactions?|payments?|paid|charged|debit", t)) or merchant is not None,
             "search_transactions", {"merchant": merchant, "limit": 5}),
            (bool(re.search(r"\bcards?\b", t)), "list_cards", {}),
        ]
        for matched, name, args in candidates:
            if matched and name in tools:
                return name, {k: v for k, v in args.items() if v is not None}
        return None

    def _follow_up(self, text: str, tool: str, payload: dict[str, Any],
                   tools: set[str]) -> tuple[str, dict[str, Any]] | None:
        t = text.lower()
        if tool == "search_transactions" and ("dispute" in t or "recogni" in t) and "raise_dispute" in tools:
            if payload.get("transactions"):
                reason = ("duplicate" if "twice" in t or "duplicate" in t
                          else "cancelled_subscription" if "cancel" in t else "not_recognised")
                return "raise_dispute", {"transaction_id": payload["transactions"][0]["transaction_id"],
                                         "reason": reason, "description": "Raised via assistant"}
        if tool == "list_cards" and "reissue_card" in tools and re.search(r"lost|stolen|damaged|replace|broken|"
                                                                          r"arrived|new card", t):
            active = [c for c in payload.get("cards", []) if c["status"] == "active"]
            if active:
                reason = next((r for r in ("stolen", "lost", "damaged") if r in t),
                              "not_received" if "arrived" in t else "damaged")
                return "reissue_card", {"card_ref": active[0]["card_ref"], "reason": reason}
        return None

    @staticmethod
    def _kyc_args(text: str) -> dict[str, Any]:
        lowered = text.lower()
        field = "address" if "address" in lowered else "email" if "email" in lowered else "phone"
        m = re.search(r"\bto\s+(.+)$", text, re.I | re.S)
        return {"field": field, "new_value": m.group(1).strip().rstrip(".") if m else None}

    # ---- phrasing (only from tool data) ----------------------------------------------------
    @staticmethod
    def _phrase(tool: str, p: dict[str, Any]) -> str:
        match tool:
            case "get_balance":
                return (f"Your {p['name']} ({p['masked_number']}) balance is £{p['balance']}. "
                        f"You have £{p['available']} available, including your £{p['overdraft_limit']} overdraft.")
            case "list_accounts":
                lines = [f"- {a['name']} ({a['masked_number']}): £{a['balance']}" for a in p["accounts"]]
                return "Here are your accounts:\n" + "\n".join(lines)
            case "search_transactions":
                if not p["transactions"]:
                    return "I couldn't find any matching transactions."
                lines = [f"- {t['date']} {t['merchant']}: £{t['amount']} ({t['direction']})"
                         for t in p["transactions"]]
                return f"Here are the {p['count']} most recent matching transactions:\n" + "\n".join(lines)
            case "spending_summary":
                parts = ", ".join(f"{k.replace('_', ' ')} £{v}" for k, v in list(p["by_category"].items())[:5])
                return f"In {p['month']} you spent £{p['total']} in total. Biggest categories: {parts}."
            case "list_cards":
                return "Your cards: " + ", ".join(f"card ending {c['last4']} ({c['status']}, expires {c['expiry']})"
                                                  for c in p["cards"])
            case "get_profile_summary":
                return (f"We have {p['email_on_file']} and phone {p['phone_on_file']} on file, "
                        f"with postcode {p['postcode_on_file']}.")
            case "get_dispute_status":
                if not p["disputes"]:
                    return "You don't have any open disputes."
                return "Your disputes: " + "; ".join(f"{d['reference']} is {d['status']}" for d in p["disputes"])
            case "get_service_request_status":
                if not p["requests"]:
                    return "You don't have any open service requests."
                return "Your requests: " + "; ".join(f"{r['reference']} ({r['kind'].replace('_', ' ')}) is "
                                                     f"{r['status']}" for r in p["requests"])
            case _:
                return "Done."

    # ---- response builders -----------------------------------------------------------------
    def _reply(self, text: str) -> LLMResponse:
        return LLMResponse(provider=self.name, model="rule-based", text=text, tool_calls=[], stop_reason="end_turn",
                           usages=[Usage(model="rule-based")])

    def _call(self, name: str, args: dict[str, Any]) -> LLMResponse:
        call = ToolCall(id=f"rb_{uuid.uuid4().hex[:10]}", name=name, arguments=args)
        return LLMResponse(provider=self.name, model="rule-based", text="", tool_calls=[call],
                           stop_reason="tool_use", usages=[Usage(model="rule-based")])
