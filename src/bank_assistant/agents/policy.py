"""Customer confirmation for state-changing actions.

The model can propose an action; it can't carry one out. When a domain agent asks for a write tool, the
agent service runs it as a dry run (the MCP server validates it and describes it), stores the exact
arguments as a pending action, and asks the customer to confirm. Only a clear "yes" on the next turn
executes those stored arguments. Nothing the model writes later can change them.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

_AFFIRM = r"(?:yes|y|yeah|yep|confirm(?:ed)?|go ahead|please do|do it|ok(?:ay)?|sure|please|thanks|thank you)"
# The whole reply must be affirmative words: "yes please" confirms, "yes but don't block it" doesn't.
_YES = re.compile(rf"^\s*{_AFFIRM}(?:[\s,]+{_AFFIRM})*[\s.!]*$", re.I)
_NO = re.compile(r"^\s*(?:no|n|nope|cancel|stop|don'?t|do not)\b", re.I)

ACTION_LABELS = {
    "statement": "Request a statement",
    "dispute": "Raise a dispute",
    "card_reissue": "Order a replacement card",
    "cheque_book": "Order a cheque book",
    "kyc_update": "Start a change to your contact details",
}

RESULT_LABELS = {
    "statement": "Your statement request",
    "dispute": "Your dispute",
    "card_reissue": "Your replacement card order",
    "cheque_book": "Your cheque book order",
    "kyc_update": "Your contact details change",
}

PENDING_TTL_SECONDS = 600

# Actions that need strong customer authentication (PSD2 SCA style) on top of the "yes": a one-time code on the
# customer's registered device, exchanged for a step-up token bound to this exact action.
HIGH_RISK_TOOLS = frozenset({"reissue_card", "submit_kyc_update"})
SCA_ACR = "urn:bank:sca:otp"


def action_binding(tool: str, arguments: dict[str, Any]) -> str:
    """'Dynamic linking': a fingerprint of exactly what will run. The step-up token must carry the same value, so a
    code approved for one action can't authorise a different one."""
    canonical = json.dumps({"tool": tool, "arguments": arguments}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


@dataclass
class PendingAction:
    domain: str
    tool: str
    arguments: dict[str, Any]
    summary: str
    action: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    sca_required: bool = False
    binding: str = ""

    def __post_init__(self) -> None:
        if not self.binding:
            self.binding = action_binding(self.tool, self.arguments)
        if self.tool in HIGH_RISK_TOOLS:
            self.sca_required = True

    def public(self) -> dict[str, Any]:
        """What the client may see: enough to start a step-up, nothing about the arguments."""
        return {"id": self.id, "action": self.action, "sca_required": self.sca_required, "binding": self.binding}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingAction:
        return cls(**data)

    def expired(self, now: float | None = None) -> bool:
        return (now or time.time()) - self.created_at > PENDING_TTL_SECONDS


def classify_reply(text: str) -> str:
    """'yes', 'no' or 'other'. Deliberately strict: anything ambiguous is not a confirmation."""
    if _YES.match(text) and not re.fullmatch(r"\s*(?:please|thanks|thank you)[\s.!]*", text, re.I):
        return "yes"
    if _NO.match(text):
        return "no"
    return "other"


def summarise_preview(preview: dict[str, Any]) -> tuple[str, str]:
    """Build the confirmation text from the server's validated preview, never from model text."""
    action = str(preview.get("action", "request"))
    details = preview.get("details", {}) or {}
    parts = "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in details.items() if v)
    return action, f"{ACTION_LABELS.get(action, 'Submit a request')}. {parts}."


def confirmation_prompt(summary: str, sca_required: bool = False) -> str:
    if sca_required:
        return (f"Please check this before I go ahead:\n{summary}\nThis change needs extra security: approve it in the "
                "app with the one-time code we send to your phone, or reply NO to cancel.")
    return f"Please check this before I go ahead:\n{summary}\nReply YES to confirm or NO to cancel."


def result_message(result: dict[str, Any]) -> str:
    action = RESULT_LABELS.get(str(result.get("action")), "Your request")
    reference = result.get("reference", "")
    extra = result.get("details", {}) or {}
    next_step = extra.get("next_step") or extra.get("delivery") or ""
    message = f"Done. {action} has been submitted. Your reference is {reference}."
    if next_step:
        message += f" Next: {next_step}."
    if result.get("advice"):
        message += f" {result['advice']}"
    return message
