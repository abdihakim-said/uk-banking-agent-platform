"""Claude via the official Anthropic SDK.

Choices that matter here:
  * ``fallbacks="default"`` (beta ``server-side-fallback-2026-07-01``): if Claude's safety classifiers decline
    a request, the API re-runs it on Anthropic's recommended fallback model inside the same call. A refusal
    that survives the chain comes back as ``stop_reason="refusal"`` and is handled by the agent.
  * Thinking is always on for Claude Opus 5.5; depth is set with ``output_config.effort``.
  * Tool choice stays ``auto``: forced tool use is rejected on this model.
  * The system prompt is cached (``cache_control``), since it's identical on every call for an agent.
  * Usage is read per iteration so a fallback hop is priced at the model that actually ran.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from bank_assistant.llm.types import (
    AssistantMessage,
    LLMResponse,
    Message,
    ProviderError,
    ProviderUnavailableError,
    StopReason,
    ToolCall,
    ToolResultMessage,
    ToolSpec,
    Usage,
    UserMessage,
)

_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", max_tokens: int = 16000,
                 timeout_seconds: float = 60.0, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.client = client or anthropic.AsyncAnthropic(timeout=timeout_seconds, max_retries=2)

    def _messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if isinstance(m, UserMessage):
                out.append({"role": "user", "content": m.text})
            elif isinstance(m, AssistantMessage):
                if m.raw is not None and m.provider == self.name:
                    out.append({"role": "assistant", "content": m.raw})
                else:
                    out.append({"role": "assistant", "content": m.text or "(no text)"})
            elif isinstance(m, ToolResultMessage):
                out.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content, "is_error": r.is_error}
                    for r in m.results
                ]})
        return out

    async def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec] | None = None,
                       json_schema: dict[str, Any] | None = None, max_tokens: int | None = None) -> LLMResponse:
        output_config: dict[str, Any] = {"effort": self.effort}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": self._messages(messages),
            "output_config": output_config,
            "betas": [_FALLBACK_BETA],
            "fallbacks": "default",
        }
        if tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                               for t in tools]
        try:
            response = await self.client.beta.messages.create(**kwargs)
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError,
                anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise ProviderUnavailableError(f"anthropic: {type(exc).__name__}") from exc
        except TypeError as exc:
            # The SDK raises TypeError when no credentials can be resolved (no key, token or `ant auth` profile).
            if "authentication method" in str(exc):
                raise ProviderUnavailableError("anthropic: no credentials configured") from exc
            raise
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise ProviderUnavailableError(f"anthropic: HTTP {exc.status_code}") from exc
            raise ProviderError(f"anthropic: HTTP {exc.status_code}: {exc.message}") from exc

        stop: StopReason
        match response.stop_reason:
            case "end_turn" | "stop_sequence":
                stop = "end_turn"
            case "tool_use":
                stop = "tool_use"
            case "max_tokens":
                stop = "max_tokens"
            case "refusal":
                stop = "refusal"
            case _:
                stop = "other"

        texts = [b.text for b in response.content if b.type == "text"]
        calls = [ToolCall(id=b.id, name=b.name, arguments=_as_dict(b.input))
                 for b in response.content if b.type == "tool_use"]
        return LLMResponse(
            provider=self.name, model=response.model, text="\n".join(texts).strip(), tool_calls=calls,
            stop_reason=stop, usages=_usages(response), raw=response.content,
            refusal_category=(response.stop_details.category
                              if stop == "refusal" and response.stop_details is not None else None),
        )


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _usages(response: Any) -> list[Usage]:
    """Per-attempt usage when the API reports iterations (fallback hops), else the top-level usage.
    Attempts are all priced, so the figure is an upper bound when a declined attempt isn't billed."""
    u = response.usage
    iterations = getattr(u, "iterations", None) or []
    priced = [it for it in iterations if getattr(it, "model", None)]
    if priced:
        return [Usage(model=it.model, input_tokens=it.input_tokens or 0, output_tokens=it.output_tokens or 0,
                      cache_creation_input_tokens=it.cache_creation_input_tokens or 0,
                      cache_read_input_tokens=it.cache_read_input_tokens or 0) for it in priced]
    return [Usage(model=response.model, input_tokens=u.input_tokens or 0, output_tokens=u.output_tokens or 0,
                  cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
                  cache_read_input_tokens=u.cache_read_input_tokens or 0)]
