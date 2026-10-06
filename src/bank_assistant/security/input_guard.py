"""Deterministic checks on customer input, run before any model call.

These are cheap tripwires, not a complete defence. The real controls are structural: the model can only
reach tools for its own domain, the customer id comes from a signed token rather than model output, and
every state-changing action needs the customer's explicit confirmation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Verdict(StrEnum):
    ALLOW = "allow"
    BLOCK_INJECTION = "block_injection"
    REGULATED_ADVICE = "regulated_advice"
    HUMAN_HANDOFF = "human_handoff"


@dataclass(frozen=True)
class GuardResult:
    verdict: Verdict
    matched: str | None = None


_INJECTION = [
    r"ignore (?:all |any |the )?(?:previous|prior|above|earlier) (?:instructions|rules|messages)",
    r"disregard (?:your|the|all) (?:instructions|rules|guidelines)",
    r"(?:reveal|show|print|repeat) (?:me )?(?:your|the) (?:system )?(?:prompt|instructions)",
    r"\bsystem prompt\b",
    r"you are now\b",
    r"\b(?:developer|dan|jailbreak|god) mode\b",
    r"\bact as (?:an? )?(?:admin|administrator|developer|bank employee|system)",
    r"(?:another|other|different) customer'?s?\b",
    r"\bcustomer[_ ]?id\b",
    r"\bcontext[_ ]?token\b",
    r"</?(?:system|tool|assistant)>",
]

# Personal recommendations on investments, pensions, mortgages or insurance are regulated advice.
_REGULATED_ADVICE = [
    r"\bshould i (?:invest|buy|sell|put (?:my )?(?:money|savings|pension))",
    r"\b(?:invest|put)\b.{0,30}\b(?:crypto|bitcoin|stocks?|shares|funds?)\b",
    r"\b(?:which|what) (?:stocks?|shares?|funds?|crypto(?:currency)?|isa) should i\b",
    r"\b(?:invest(?:ment)?|pension|mortgage|insurance) advice\b",
    r"\bis (?:it|now) a good time to (?:invest|buy)",
    r"\bbest (?:investment|pension|mortgage)\b",
]

# Signs the customer may be vulnerable or wants a person (FCA Consumer Duty, FG21/1): route to a human.
_HANDOFF = [
    r"\b(?:speak|talk) to (?:a |an )?(?:human|person|someone|agent|advisor|adviser)\b",
    r"\b(?:make|raise|file) a complaint\b|\bcomplain\b",
    r"\bbereave|\bpassed away\b",
    r"\b(?:can'?t|cannot|struggling to) (?:pay|afford)\b|\bfinancial difficult",
    r"\bdebt (?:help|advice)\b",
    r"\bfraud(?:ster)?s? (?:called|contacted|texted)\b|\bscam(?:med)?\b",
    r"\bsuicid|\bself[- ]harm\b",
]


def _first_match(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, text, re.I)
        if m:
            return m.group(0)
    return None


def check_input(text: str) -> GuardResult:
    if (m := _first_match(_INJECTION, text)) is not None:
        return GuardResult(Verdict.BLOCK_INJECTION, m)
    if (m := _first_match(_HANDOFF, text)) is not None:
        return GuardResult(Verdict.HUMAN_HANDOFF, m)
    if (m := _first_match(_REGULATED_ADVICE, text)) is not None:
        return GuardResult(Verdict.REGULATED_ADVICE, m)
    return GuardResult(Verdict.ALLOW)
