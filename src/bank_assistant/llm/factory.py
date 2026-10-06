"""Build the provider chain from settings."""

from __future__ import annotations

from bank_assistant.config import Settings
from bank_assistant.llm.anthropic_provider import AnthropicProvider
from bank_assistant.llm.private_provider import PrivateModelProvider
from bank_assistant.llm.router import LLMRouter
from bank_assistant.llm.rule_based import RuleBasedProvider
from bank_assistant.llm.types import LLMProvider
from bank_assistant.logging import get_logger

log = get_logger(__name__)


def build_router(settings: Settings) -> LLMRouter:
    providers: list[LLMProvider] = []
    for name in settings.llm_route_list:
        match name:
            case "anthropic":
                providers.append(AnthropicProvider(settings.anthropic_model, settings.anthropic_effort,
                                                   settings.anthropic_max_tokens, settings.llm_timeout_seconds))
            case "private":
                if settings.private_llm_base_url:
                    providers.append(PrivateModelProvider(settings.private_llm_base_url, settings.private_llm_model,
                                                          settings.llm_timeout_seconds))
                else:
                    log.info("private_llm_skipped", reason="BANK_PRIVATE_LLM_BASE_URL not set")
            case "rule_based":
                providers.append(RuleBasedProvider())
            case other:
                raise ValueError(f"unknown LLM provider in BANK_LLM_ROUTE: {other}")
    log.info("llm_route", providers=[p.name for p in providers])
    return LLMRouter(providers, settings.breaker_failure_threshold, settings.breaker_cooldown_seconds)
