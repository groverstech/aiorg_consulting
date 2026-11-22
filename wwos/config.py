"""Runtime configuration, loaded from environment variables.

Every setting has a safe default so the system runs locally with no setup.
Production values are supplied through the environment (see docs/RUNBOOK.md).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env("WWOS_DATABASE_URL", "sqlite:///./var/wwos.db"))
    # "offline" uses a deterministic local provider (no network, no spend).
    # "anthropic" calls the Claude API; credentials resolve from the environment.
    llm_provider: str = field(default_factory=lambda: _env("WWOS_LLM_PROVIDER", "offline"))
    default_model: str = field(default_factory=lambda: _env("WWOS_DEFAULT_MODEL", "claude-opus-5-5"))
    # Hard ceilings. The owner authorises these; agents can never raise them.
    org_monthly_budget_usd: Decimal = field(
        default_factory=lambda: Decimal(_env("WWOS_ORG_MONTHLY_BUDGET_USD", "500"))
    )
    max_agents_per_tenant: int = field(default_factory=lambda: int(_env("WWOS_MAX_AGENTS_PER_TENANT", "40")))
    max_agent_budget_usd: Decimal = field(
        default_factory=lambda: Decimal(_env("WWOS_MAX_AGENT_BUDGET_USD", "100"))
    )
    agent_failure_suspend_threshold: int = field(
        default_factory=lambda: int(_env("WWOS_AGENT_FAILURE_SUSPEND_THRESHOLD", "3"))
    )
    # Business policy: whether outbound email to external parties needs owner approval.
    # Defaults to True until the owner decides otherwise (see docs/decisions/ADR-006).
    external_email_requires_approval: bool = field(
        default_factory=lambda: _env_bool("WWOS_EXTERNAL_EMAIL_REQUIRES_APPROVAL", True)
    )
    email_transport: str = field(default_factory=lambda: _env("WWOS_EMAIL_TRANSPORT", "outbox"))
    smtp_host: str = field(default_factory=lambda: _env("WWOS_SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: int(_env("WWOS_SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: _env("WWOS_SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: _env("WWOS_SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: _env("WWOS_SMTP_FROM", ""))
    task_max_attempts: int = field(default_factory=lambda: int(_env("WWOS_TASK_MAX_ATTEMPTS", "4")))
    task_lease_seconds: int = field(default_factory=lambda: int(_env("WWOS_TASK_LEASE_SECONDS", "300")))
    admin_token: str = field(default_factory=lambda: _env("WWOS_ADMIN_TOKEN", ""))


def get_settings() -> Settings:
    return Settings()
