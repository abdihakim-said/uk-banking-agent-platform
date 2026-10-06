"""Self-hosted models (vLLM or Ollama) through their OpenAI-compatible ``/v1/chat/completions`` endpoint.

Used when data must stay inside the bank's network, or as a fallback when the commercial API is down.
Small open models are much weaker at multi-step tool use, so the evaluation suite should be run against
whichever model is configured here before relying on it.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

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


class PrivateModelProvider:
    name = "private"

    def __init__(self, base_url: str, model: str, timeout_seconds: float = 60.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self.model = model
        self.url = base_url.rstrip("/") + "/v1/chat/completions"
        self.client = client or httpx.AsyncClient(timeout=timeout_seconds)

    def _messages(self, system: str, messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if isinstance(m, UserMessage):
                out.append({"role": "user", "content": m.text})
            elif isinstance(m, AssistantMessage):
                entry: dict[str, Any] = {"role": "assistant", "content": m.text or ""}
                if m.tool_calls:
                    entry["tool_calls"] = [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                        for c in m.tool_calls
                    ]
                out.append(entry)
            elif isinstance(m, ToolResultMessage):
                out.extend({"role": "tool", "tool_call_id": r.call_id, "content": r.content} for r in m.results)
        return out

    async def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec] | None = None,
                       json_schema: dict[str, Any] | None = None, max_tokens: int | None = None) -> LLMResponse:
        body: dict[str, Any] = {"model": self.model, "messages": self._messages(system, messages),
                                "max_tokens": max_tokens or 2048, "temperature": 0}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                               "parameters": t.input_schema}} for t in tools]
        if json_schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "result", "schema": json_schema, "strict": True}}
        try:
            resp = await self.client.post(self.url, json=body)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"private: {type(exc).__name__}") from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise ProviderUnavailableError(f"private: HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise ProviderError(f"private: HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        choice = data["choices"][0]
        message = choice.get("message", {})
        calls: list[ToolCall] = []
        for c in message.get("tool_calls") or []:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError as exc:
                raise ProviderError("private: tool arguments were not valid JSON") from exc
            calls.append(ToolCall(id=c.get("id") or f"call_{len(calls)}", name=c["function"]["name"],
                                  arguments=args if isinstance(args, dict) else {}))
        finish = choice.get("finish_reason")
        stop: StopReason = ("tool_use" if calls else "end_turn" if finish in ("stop", None)
                            else "max_tokens" if finish == "length" else "other")
        usage = data.get("usage") or {}
        return LLMResponse(
            provider=self.name, model=data.get("model", self.model), text=(message.get("content") or "").strip(),
            tool_calls=calls, stop_reason=stop,
            usages=[Usage(model=f"private:{self.model}", input_tokens=usage.get("prompt_tokens", 0),
                          output_tokens=usage.get("completion_tokens", 0))],
            raw=None,
        )
