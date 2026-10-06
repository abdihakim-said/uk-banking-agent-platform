from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaMessage

from bank_assistant.llm.anthropic_provider import AnthropicProvider
from bank_assistant.llm.private_provider import PrivateModelProvider
from bank_assistant.llm.router import AllProvidersUnavailableError, LLMRouter
from bank_assistant.llm.types import (
    AssistantMessage,
    LLMResponse,
    Message,
    ProviderError,
    ProviderUnavailableError,
    ToolCall,
    ToolResult,
    ToolResultMessage,
    ToolSpec,
    Usage,
    UserMessage,
)
from bank_assistant.observability.cost import PriceTable

TOOL = ToolSpec("get_balance", "Get a balance", {"type": "object", "properties": {}})


class FakeMessages:
    def __init__(self, response: BetaMessage | Exception) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> BetaMessage:
        self.kwargs = kwargs
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeClient:
    def __init__(self, response: BetaMessage | Exception) -> None:
        self.beta = type("Beta", (), {})()
        self.beta.messages = FakeMessages(response)


def beta_message(content: list[dict[str, Any]], stop_reason: str, **extra: Any) -> BetaMessage:
    return BetaMessage.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 80, "cache_read_input_tokens": 1000,
                  "cache_creation_input_tokens": 0},
        **extra,
    })


async def test_anthropic_request_shape_and_tool_call() -> None:
    client = FakeClient(beta_message(
        [{"type": "thinking", "thinking": "", "signature": "sig"},
         {"type": "tool_use", "id": "toolu_1", "name": "get_balance", "input": {"account_ref": "acc_1"}}],
        "tool_use"))
    provider = AnthropicProvider(client=client)  # type: ignore[arg-type]
    resp = await provider.complete(system="sys", messages=[UserMessage("balance?")], tools=[TOOL])
    sent = client.beta.messages.kwargs
    assert sent["model"] == "claude-opus-5-5"
    assert sent["fallbacks"] == "default" and sent["betas"] == ["server-side-fallback-2026-07-01"]
    assert sent["output_config"] == {"effort": "medium"}
    assert "tool_choice" not in sent and "thinking" not in sent
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls == [ToolCall("toolu_1", "get_balance", {"account_ref": "acc_1"})]
    assert resp.usages == [Usage("claude-opus-5-5", 1200, 80, 0, 1000)]

    # Native content (including the thinking block) is replayed unchanged within the turn.
    follow_up = provider._messages([UserMessage("q"), resp.as_message(),
                                    ToolResultMessage([ToolResult("toolu_1", '{"balance": "1.00"}')])])
    assert follow_up[1]["content"] is resp.raw
    assert follow_up[2]["content"][0]["tool_use_id"] == "toolu_1"


async def test_anthropic_refusal_is_reported_not_raised() -> None:
    client = FakeClient(beta_message([], "refusal", stop_details={"type": "refusal", "category": "cyber",
                                                                  "explanation": None}))
    resp = await AnthropicProvider(client=client).complete(system="s", messages=[UserMessage("x")])  # type: ignore[arg-type]
    assert resp.stop_reason == "refusal" and resp.refusal_category == "cyber"


async def test_anthropic_structured_output_requested() -> None:
    client = FakeClient(beta_message([{"type": "text", "text": '{"route": "accounts", "reason": "r"}'}], "end_turn"))
    schema = {"type": "object", "properties": {}}
    resp = await AnthropicProvider(client=client).complete(system="s", messages=[UserMessage("x")],  # type: ignore[arg-type]
                                                           json_schema=schema)
    assert client.beta.messages.kwargs["output_config"]["format"] == {"type": "json_schema", "schema": schema}
    assert json.loads(resp.text)["route"] == "accounts"


async def test_anthropic_errors_are_classified() -> None:
    import anthropic

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    unavailable = anthropic.APIConnectionError(request=request)
    bad = anthropic.BadRequestError("bad", response=httpx.Response(400, request=request), body=None)
    with pytest.raises(ProviderUnavailableError):
        await AnthropicProvider(client=FakeClient(unavailable)).complete(system="s", messages=[])  # type: ignore[arg-type]
    with pytest.raises(ProviderError):
        await AnthropicProvider(client=FakeClient(bad)).complete(system="s", messages=[])  # type: ignore[arg-type]


async def test_anthropic_missing_credentials_is_unavailable() -> None:
    no_creds = TypeError("Could not resolve authentication method. Expected one of api_key, auth_token")
    with pytest.raises(ProviderUnavailableError):
        await AnthropicProvider(client=FakeClient(no_creds)).complete(system="s", messages=[])  # type: ignore[arg-type]


async def test_private_provider_tool_calls_round_trip() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"model": "llama", "choices": [{"finish_reason": "tool_calls", "message": {
            "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "get_balance", "arguments": "{\"account_ref\": \"acc_1\"}"}}]}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 9}})

    provider = PrivateModelProvider("http://vllm:8000", "llama",
                                    client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    history: list[Message] = [UserMessage("hi"), AssistantMessage("", [ToolCall("c0", "list_accounts", {})]),
                              ToolResultMessage([ToolResult("c0", "{}")])]
    resp = await provider.complete(system="sys", messages=history, tools=[TOOL])
    assert resp.tool_calls == [ToolCall("c1", "get_balance", {"account_ref": "acc_1"})]
    assert resp.usages[0].model == "private:llama"
    assert [m["role"] for m in captured["messages"]] == ["system", "user", "assistant", "tool"]
    assert captured["tools"][0]["function"]["name"] == "get_balance"


async def test_private_provider_5xx_is_unavailable() -> None:
    provider = PrivateModelProvider("http://ollama:11434", "m", client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    with pytest.raises(ProviderUnavailableError):
        await provider.complete(system="s", messages=[UserMessage("x")])


class Flaky:
    def __init__(self, name: str, fail: bool) -> None:
        self.name, self.fail, self.calls = name, fail, 0

    async def complete(self, **_: Any) -> LLMResponse:
        self.calls += 1
        if self.fail:
            raise ProviderUnavailableError("down")
        return LLMResponse(self.name, "m", "ok", [], "end_turn", [])


async def test_router_falls_back_and_opens_circuit() -> None:
    clock = [0.0]
    primary, backup = Flaky("primary", True), Flaky("backup", False)
    router = LLMRouter([primary, backup], failure_threshold=2, cooldown_seconds=30, clock=lambda: clock[0])
    for _ in range(3):
        assert (await router.complete(system="s", messages=[])).provider == "backup"
    assert primary.calls == 2 and router.is_open("primary")  # third call skipped the open circuit
    clock[0] = 31
    primary.fail = False
    assert (await router.complete(system="s", messages=[])).provider == "primary"  # half-open trial succeeded


async def test_router_pinned_provider_does_not_fall_back() -> None:
    router = LLMRouter([Flaky("primary", True), Flaky("backup", False)])
    with pytest.raises(AllProvidersUnavailableError):
        await router.complete(system="s", messages=[], pinned="primary")


def test_price_table() -> None:
    prices = PriceTable()
    cost = prices.cost(Usage("claude-opus-5-5", input_tokens=1_000_000, output_tokens=100_000,
                             cache_creation_input_tokens=200_000, cache_read_input_tokens=1_000_000))
    assert cost == Decimal("4") + Decimal("2") + Decimal("1") + Decimal("0.2")
    assert prices.cost(Usage("private:llama", 10**6, 10**6)) == 0
    assert prices.cost(Usage("claude-unknown", 1_000_000)) == Decimal("10")  # priced at the dearest known input
