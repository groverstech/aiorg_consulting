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


def _database_url() -> str:
    """WWOS_DATABASE_URL wins. On Cloud Run the URL is assembled from the Cloud SQL unix socket and the
    password secret, so the password never appears in configuration or Terraform state."""
    explicit = os.environ.get("WWOS_DATABASE_URL")
    if explicit:
        return explicit
    socket_dir = os.environ.get("WWOS_DATABASE_HOST_SOCKET")
    if socket_dir:
        from urllib.parse import quote_plus

        user = os.environ.get("WWOS_DB_USER", "wwos")
        name = os.environ.get("WWOS_DB_NAME", "wwos")
        password = quote_plus(os.environ.get("WWOS_DB_PASSWORD", ""))
        return f"postgresql+psycopg://{user}:{password}@/{name}?host={quote_plus(socket_dir)}"
    return "sqlite:///./var/wwos.db"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # "development" (default) or "production". Production refuses unsafe configuration.
    environment: str = field(default_factory=lambda: _env("WWOS_ENV", "development"))
    database_url: str = field(default_factory=_database_url)
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
    # starttls (port 587) | ssl (implicit TLS, port 465) | none (local testing only; refused in production)
    smtp_security: str = field(default_factory=lambda: _env("WWOS_SMTP_SECURITY", "starttls"))
    smtp_timeout_seconds: float = field(default_factory=lambda: float(_env("WWOS_SMTP_TIMEOUT_SECONDS", "30")))
    task_max_attempts: int = field(default_factory=lambda: int(_env("WWOS_TASK_MAX_ATTEMPTS", "4")))
    task_lease_seconds: int = field(default_factory=lambda: int(_env("WWOS_TASK_LEASE_SECONDS", "900")))
    # Grants access to /health/details (cross-tenant queue counts). Empty = endpoint disabled.
    admin_token: str = field(default_factory=lambda: _env("WWOS_ADMIN_TOKEN", ""))
    # Set true behind TLS so the dashboard session cookie is only sent over HTTPS.
    cookie_secure: bool = field(default_factory=lambda: _env_bool("WWOS_COOKIE_SECURE", False))
    # Public origin users reach the dashboard at, e.g. "https://ops.whitewaters.example". Used for
    # cross-site request checks behind a TLS proxy. Empty = compare the Origin host to the Host header.
    public_origin: str = field(default_factory=lambda: _env("WWOS_PUBLIC_ORIGIN", "").rstrip("/"))


def get_settings() -> Settings:
    return Settings()


class ConfigurationError(RuntimeError):
    pass


def production_problems(cfg: Settings | None = None) -> list[str]:
    """Unsafe settings that production must refuse. Empty list = acceptable."""
    cfg = cfg or get_settings()
    problems = []
    if not cfg.database_url.startswith("postgresql"):
        problems.append("WWOS_DATABASE_URL must be PostgreSQL in production (SQLite is single-process, dev only)")
    if cfg.llm_provider not in ("anthropic", "offline"):
        problems.append("WWOS_LLM_PROVIDER must be 'anthropic' or 'offline'")
    if cfg.email_transport == "smtp":
        if cfg.smtp_security == "none":
            problems.append("WWOS_SMTP_SECURITY=none is not allowed in production")
        if not (cfg.smtp_host and cfg.smtp_from):
            problems.append("WWOS_SMTP_HOST and WWOS_SMTP_FROM are required for SMTP")
    if not cfg.cookie_secure:
        problems.append("WWOS_COOKIE_SECURE must be true in production (dashboard served over HTTPS)")
    if not cfg.public_origin.startswith("https://"):
        problems.append("WWOS_PUBLIC_ORIGIN must be the https:// URL of the dashboard")
    if cfg.admin_token and len(cfg.admin_token) < 32:
        problems.append("WWOS_ADMIN_TOKEN must be at least 32 characters (or empty to disable)")
    if cfg.org_monthly_budget_usd <= 0 or cfg.max_agent_budget_usd <= 0:
        problems.append("budget ceilings must be positive")
    return problems


def assert_safe_for_environment() -> None:
    cfg = get_settings()
    if cfg.environment != "production":
        return
    problems = production_problems(cfg)
    if problems:
        raise ConfigurationError("refusing to start in production: " + "; ".join(problems))
