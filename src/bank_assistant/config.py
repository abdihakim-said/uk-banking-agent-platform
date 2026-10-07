"""Runtime configuration, read from environment variables prefixed with ``BANK_``."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_SECRET = "dev-only-change-me-dev-only-change-me"

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BANK_", env_file=".env", extra="ignore")

    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    # Storage. In deployments: rediss:// (TLS) plus a password; Redis also requires our client certificate.
    redis_url: str = "redis://localhost:6379/0"
    redis_password: SecretStr | None = None
    redis_tls_ca_file: str | None = None  # defaults to tls_ca_file
    session_ttl_seconds: int = 1800
    history_max_messages: int = 20

    # Service-to-service secrets. Defaults only work outside prod (see validator below).
    context_token_secret: SecretStr = SecretStr(_DEV_SECRET)
    internal_service_token: SecretStr = SecretStr(_DEV_SECRET)
    context_token_ttl_seconds: int = 120
    audit_hmac_key: SecretStr = SecretStr(_DEV_SECRET)  # signs the audit chain; without it entries can't be forged

    # Mutual TLS between services (see core/tls.py). Off for in-process tests; on in compose and Kubernetes.
    tls_enabled: bool = False
    tls_cert_file: str = "/certs/tls.crt"
    tls_key_file: str = "/certs/tls.key"
    tls_ca_file: str = "/certs/ca.crt"

    # Topology
    agent_service_url: str = "http://localhost:8001"
    mcp_accounts_url: str = "http://localhost:9101/mcp"
    mcp_transactions_url: str = "http://localhost:9102/mcp"
    mcp_service_url: str = "http://localhost:9103/mcp"

    # LLM routing: providers are tried in this order; a provider is skipped while its circuit is open.
    llm_route: str = "anthropic,private,rule_based"
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: Effort = "medium"
    anthropic_max_tokens: int = 16000
    private_llm_base_url: str | None = None  # e.g. http://ollama:11434 or http://vllm:8000
    private_llm_model: str = "llama3.1:8b"
    llm_timeout_seconds: float = 60.0
    breaker_failure_threshold: int = 3
    breaker_cooldown_seconds: float = 30.0

    # Gateway: OIDC
    oidc_issuer: str = "http://localhost:8000/dev-idp"
    oidc_audience: str = "uk-banking-assistant"
    oidc_jwks_url: str | None = None
    oidc_dev_public_key_path: str | None = ".dev-keys/public.pem"
    oidc_required_scope: str = "banking.chat"

    # Gateway: abuse controls
    rate_limit_per_minute: int = 20
    rate_limit_per_ip_per_minute: int = 60
    max_message_chars: int = 2000

    # FinOps
    session_budget_usd: Decimal = Decimal("0.50")
    customer_daily_budget_usd: Decimal = Decimal("5.00")

    # Observability
    otel_exporter_otlp_endpoint: str | None = None
    metrics_port: int | None = 9464  # Prometheus scrape port, separate from the API port; None disables
    service_name: str = "bank-assistant"

    @property
    def llm_route_list(self) -> list[str]:
        return [p.strip() for p in self.llm_route.split(",") if p.strip()]

    @model_validator(mode="after")
    def _no_dev_secrets_in_prod(self) -> Settings:
        if self.environment == "prod":
            for name in ("context_token_secret", "internal_service_token", "audit_hmac_key"):
                value: SecretStr = getattr(self, name)
                if value.get_secret_value() == _DEV_SECRET or len(value.get_secret_value()) < 32:
                    raise ValueError(f"BANK_{name.upper()} must be set to a strong secret in prod")
            if self.oidc_jwks_url is None:
                raise ValueError("BANK_OIDC_JWKS_URL is required in prod; the dev key file is for local use only")
            if not self.tls_enabled:
                raise ValueError("BANK_TLS_ENABLED must be true in prod: every internal hop uses mutual TLS")
            if not self.redis_url.startswith("rediss://") or self.redis_password is None:
                raise ValueError("prod Redis must use TLS (rediss://) and a password (BANK_REDIS_PASSWORD)")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
