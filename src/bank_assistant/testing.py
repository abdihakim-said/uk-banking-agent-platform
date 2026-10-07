"""Wire the whole platform together in one process: gateway → agent service → three MCP servers, on
fakeredis, with any LLM provider. Used by the test suite, the eval runner and ``make demo``."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from redis.asyncio import Redis

from bank_assistant.agents.graph import AgentDeps, AgentEngine
from bank_assistant.config import Settings
from bank_assistant.core.audit import AuditLog
from bank_assistant.core.bank_data import CoreBankingStore
from bank_assistant.core.session_store import SessionStore
from bank_assistant.gateway.app import create_app as create_gateway
from bank_assistant.gateway.auth import JwtVerifier
from bank_assistant.llm.router import LLMRouter
from bank_assistant.llm.rule_based import RuleBasedProvider
from bank_assistant.llm.types import LLMProvider, LLMResponse, Message, ToolSpec
from bank_assistant.mcp_servers.accounts_server import build_accounts_server
from bank_assistant.mcp_servers.service_server import build_service_server
from bank_assistant.mcp_servers.transactions_server import build_transactions_server
from bank_assistant.observability.cost import CostTracker
from bank_assistant.security.redactor import Redactor
from bank_assistant.services.agent_api import create_app as create_agent_app

ISSUER = "https://idp.test"
AUDIENCE = "uk-banking-assistant"
SECRET = "test-secret-0123456789abcdef0123456789"
AUDIT_KEY = "test-audit-key-0123456789abcdef0123"
NAMES = {"C001": ("Amina", "Yusuf"), "C002": ("Daniel", "Okafor")}


class RecordingProvider:
    """Wraps a provider and keeps every message sent to it, so tests can prove what the LLM saw."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.name = inner.name
        self.seen: list[str] = []

    async def complete(self, *, system: str, messages: list[Message], tools: list[ToolSpec] | None = None,
                       json_schema: dict[str, Any] | None = None, max_tokens: int | None = None) -> LLMResponse:
        self.seen.append(system)
        for m in messages:
            self.seen.append(repr(m))
        return await self.inner.complete(system=system, messages=messages, tools=tools, json_schema=json_schema,
                                         max_tokens=max_tokens)


@dataclass
class Stack:
    client: httpx.AsyncClient
    redis: Redis
    bank: CoreBankingStore
    providers: list[RecordingProvider]
    private_key: Any
    engine: AgentEngine
    tokens: dict[str, str] = field(default_factory=dict)

    def token(self, customer_id: str = "C001", **overrides: Any) -> str:
        now = int(time.time())
        given, family = NAMES.get(customer_id, ("Test", "User"))
        claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": f"user-{customer_id}", "iat": now, "exp": now + 600,
                  "scope": "banking.chat", "customer_id": customer_id, "given_name": given, "family_name": family}
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self.private_key, algorithm="RS256")

    async def chat(self, message: str, session_id: str | None = None, customer_id: str = "C001") -> httpx.Response:
        body: dict[str, Any] = {"message": message}
        if session_id:
            body["session_id"] = session_id
        return await self.client.post("/v1/chat", json=body,
                                      headers={"Authorization": f"Bearer {self.token(customer_id)}"})

    @property
    def llm_input(self) -> str:
        return "\n".join(s for p in self.providers for s in p.seen)


@asynccontextmanager
async def build_stack(providers: Sequence[LLMProvider] | None = None, today: date | None = None,
                      settings_overrides: dict[str, Any] | None = None) -> AsyncIterator[Stack]:
    import fakeredis

    redis = fakeredis.FakeAsyncRedis()
    settings = Settings(environment="test", context_token_secret=SECRET, internal_service_token=SECRET,  # type: ignore[arg-type]
                        oidc_issuer=ISSUER, oidc_audience=AUDIENCE, **(settings_overrides or {}))
    bank = CoreBankingStore(today=today)
    recording = [RecordingProvider(p) for p in (providers or [RuleBasedProvider(today=bank.today)])]
    engine = AgentEngine(AgentDeps(
        router=LLMRouter(recording, failure_threshold=2, cooldown_seconds=30),
        store=SessionStore(redis, settings.session_ttl_seconds, settings.history_max_messages),
        costs=CostTracker(redis, session_budget=settings.session_budget_usd,
                          customer_daily_budget=settings.customer_daily_budget_usd),
        redactor=Redactor(),
        mcp_targets={"accounts": build_accounts_server(bank, SECRET),
                     "transactions": build_transactions_server(bank, SECRET),
                     "service": build_service_server(bank, SECRET)},
        context_secret=SECRET,
        today=lambda: bank.today,
        audit=AuditLog(redis, AUDIT_KEY, "agent"),
    ))
    agent_app = create_agent_app(engine=engine, settings=settings)
    agent_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_app), base_url="http://agent")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    gateway = create_gateway(settings=settings, redis=redis, agent_client=agent_client, redactor=Redactor(),
                             audit=AuditLog(redis, AUDIT_KEY, "gateway"),
                             verifier=JwtVerifier(ISSUER, AUDIENCE, "banking.chat", public_key_pem=public_pem))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway), base_url="http://gateway") as client:
        yield Stack(client=client, redis=redis, bank=bank, providers=recording, private_key=key, engine=engine)
    await agent_client.aclose()
