"""The domain agent loop: model → tools → model, restricted to one MCP server.

Rules enforced here, whatever the model does:
  * Budgets are checked before every model call.
  * Placeholders are swapped for real values only in tool arguments; tool results are redacted again before
    the model sees them.
  * A write tool is never executed in the loop. It runs as a dry run and becomes a pending action that the
    customer must confirm on their next message.
  * The loop stops after ``MAX_STEPS`` model calls.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from bank_assistant.agents.policy import PendingAction, confirmation_prompt, summarise_preview
from bank_assistant.agents.prompts import REPLIES
from bank_assistant.agents.toolbox import McpToolbox
from bank_assistant.llm.router import LLMRouter
from bank_assistant.llm.types import Message, ToolResult, ToolResultMessage, Usage
from bank_assistant.security.redactor import PiiVault, Redactor

MAX_STEPS = 6


@dataclass
class DomainResult:
    reply: str
    outcome: str  # answered | confirmation_required | refusal | step_limit
    tool_payloads: list[Any] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    pending: PendingAction | None = None
    provider: str | None = None
    usages: list[Usage] = field(default_factory=list)


async def run_domain_agent(
    *,
    domain: str,
    system: str,
    messages: list[Message],
    toolbox: McpToolbox,
    router: LLMRouter,
    context_token: str,
    vault: PiiVault,
    redactor: Redactor,
    known_names: list[str],
    before_model_call: Callable[[], Awaitable[None]],
    after_model_call: Callable[[list[Usage]], Awaitable[None]],
) -> DomainResult:
    result = DomainResult(reply="", outcome="answered")
    pinned: str | None = None
    for _ in range(MAX_STEPS):
        await before_model_call()
        response = await router.complete(system=system, messages=messages, tools=toolbox.specs, pinned=pinned,
                                         purpose=f"{domain}_agent")
        pinned = response.provider
        result.provider = response.provider
        result.usages.extend(response.usages)
        await after_model_call(response.usages)

        if response.stop_reason == "refusal":
            result.reply, result.outcome = REPLIES["refusal"], "refusal"
            return result
        if not response.tool_calls:
            result.reply = response.text or REPLIES["step_limit"]
            return result

        messages.append(response.as_message())
        tool_results: list[ToolResult] = []
        for call in response.tool_calls:
            if call.name not in toolbox.tool_names:
                # Least privilege: a tool from another domain (or an invented one) is refused, not routed.
                result.tool_calls.append({"tool": call.name, "write": False, "is_error": True, "refused": True})
                tool_results.append(ToolResult(call.id, f"Tool '{call.name}' is not available here.", is_error=True))
                continue
            args = vault.reveal_json(call.arguments)
            record: dict[str, Any] = {"tool": call.name, "write": toolbox.is_write(call.name)}
            if toolbox.is_write(call.name):
                preview = await toolbox.call(call.name, args, context_token, dry_run=True)
                record["dry_run"] = True
                record["is_error"] = preview.is_error
                result.tool_calls.append(record)
                if preview.is_error or preview.data is None:
                    tool_results.append(ToolResult(call.id, redactor.redact(preview.text, vault, known_names),
                                                   is_error=True))
                    continue
                action, summary = summarise_preview(preview.data)
                result.pending = PendingAction(domain=domain, tool=call.name, arguments=args, summary=summary,
                                               action=action)
                result.reply, result.outcome = confirmation_prompt(summary), "confirmation_required"
                return result

            outcome = await toolbox.call(call.name, args, context_token)
            record["is_error"] = outcome.is_error
            result.tool_calls.append(record)
            if outcome.data is not None:
                result.tool_payloads.append(outcome.data)
            safe = (json.dumps(redactor.redact_json(outcome.data, vault, known_names)) if outcome.data is not None
                    else redactor.redact(outcome.text, vault, known_names))
            tool_results.append(ToolResult(call.id, safe, is_error=outcome.is_error))
        messages.append(ToolResultMessage(tool_results))

    result.reply, result.outcome = REPLIES["step_limit"], "step_limit"
    return result
