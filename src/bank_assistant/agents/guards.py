"""Output checks on every reply before it leaves the agent service.

1. Groundedness: every money amount in the reply must appear in this turn's tool results (or the
   customer's own message). A reply that states a figure no tool returned is replaced. This also catches
   arithmetic the model did itself, which is deliberate: a bank shouldn't show figures it can't trace.
2. Advice: phrases that read as personal investment recommendations are blocked.
3. PII: any identifier the model reproduced in raw form is re-redacted (handled in the graph's finalise step).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

_MONEY = re.compile(r"£\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_ADVICE = re.compile(r"\b(?:you should (?:invest|buy|sell)|i(?:'d| would)? recommend (?:investing|buying|selling)|"
                     r"guaranteed returns?|good investment)\b", re.I)


@dataclass(frozen=True)
class GuardFinding:
    guard: str
    detail: str


def _decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def known_numbers(tool_payloads: list[Any], user_text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for chunk in [json.dumps(p) if not isinstance(p, str) else p for p in tool_payloads] + [user_text]:
        for m in _NUMBER.finditer(chunk.replace(",", "")):
            value = _decimal(m.group())
            if value is not None:
                found.add(abs(value))
    return found


def ungrounded_amounts(reply: str, allowed: set[Decimal]) -> list[str]:
    missing = []
    for m in _MONEY.finditer(reply):
        value = _decimal(m.group(1))
        if value is not None and value not in allowed:
            missing.append(m.group(0))
    return missing


def check_reply(reply: str, tool_payloads: list[Any], user_text: str) -> list[GuardFinding]:
    findings = []
    missing = ungrounded_amounts(reply, known_numbers(tool_payloads, user_text))
    if missing:
        findings.append(GuardFinding("groundedness", ", ".join(missing)))
    if _ADVICE.search(reply):
        findings.append(GuardFinding("advice", _ADVICE.search(reply).group(0)))  # type: ignore[union-attr]
    return findings
