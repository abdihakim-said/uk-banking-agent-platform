"""Provider-neutral message and response types.

Within one turn the agent keeps the provider's native assistant content (``raw``) so it can be sent back
unchanged; Claude needs its thinking blocks returned verbatim during a tool loop. Across turns only plain
text is kept, so a later turn can run on a different provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class UserMessage:
    text: str


@dataclass
class AssistantMessage:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None  # provider-native content, only valid for the provider that produced it
    provider: str | None = None


@dataclass
class ToolResultMessage:
    results: list[ToolResult]


Message = UserMessage | AssistantMessage | ToolResultMessage


@dataclass(frozen=True)
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class LLMResponse:
    provider: str
    model: str
    text: str
    tool_calls: list[ToolCall]
    stop_reason: StopReason
    usages: list[Usage]
    raw: Any = None
    refusal_category: str | None = None

    def as_message(self) -> AssistantMessage:
        return AssistantMessage(text=self.text, tool_calls=list(self.tool_calls), raw=self.raw, provider=self.provider)


class ProviderUnavailableError(Exception):
    """Transient or infrastructure failure: the router should try the next provider."""


class ProviderError(Exception):
    """The request itself is wrong (bad schema, invalid argument): don't retry elsewhere."""


class LLMProvider(Protocol):
    name: str

    async def complete(
        self,
        *,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...
