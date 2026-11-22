"""ORM models for the White Waters operating system."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, TenantScoped


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


Money = Numeric(14, 6)


class Tenant(Base):
    """A business operating on the platform (White Waters itself is tenant #1)."""

    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    monthly_budget_usd: Mapped[Decimal] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base, TenantScoped):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(20), default="owner")  # owner | operator | viewer
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WebSession(Base, TenantScoped):
    """Dashboard login session. The cookie holds a random token; only its hash is stored."""

    __tablename__ = "web_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SystemStatus(Base):
    """Platform-level key/value status shared across processes (e.g. last QA scan)."""

    __tablename__ = "system_status"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Department(Base, TenantScoped):
    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    mission: Mapped[str] = mapped_column(Text, default="")


class Agent(Base, TenantScoped):
    """A versioned AI agent. Each new version is a new row linked by ``lineage_id``."""

    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    lineage_id: Mapped[str] = mapped_column(String(36), index=True, default=new_id)
    version: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(60))  # ceo | technology | sales | specialist ...
    department_id: Mapped[str] = mapped_column(ForeignKey("departments.id"))
    instructions: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(60))
    tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    # draft -> testing -> active -> (suspended <-> active) -> retired
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    monthly_budget_usd: Mapped[Decimal] = mapped_column(Money)
    can_provision_agents: Mapped[bool] = mapped_column(Boolean, default=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(120), default="system")
    status_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CostEntry(Base, TenantScoped):
    """Immutable ledger of model spend.

    ``agent_id`` deliberately has no foreign key: the ledger records money actually spent, which
    must survive even when the agent's own creation was rolled back (e.g. a failed acceptance test).
    ``reservation_id`` is unique, so a reservation can be converted into spend exactly once.
    """

    __tablename__ = "cost_entries"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    reservation_id: Mapped[str | None] = mapped_column(String(36), nullable=True, unique=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    model: Mapped[str] = mapped_column(String(60))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class BudgetLock(Base, TenantScoped):
    """One row per tenant, locked (SELECT ... FOR UPDATE) to serialise budget reservations."""

    __tablename__ = "budget_locks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    __table_args__ = (UniqueConstraint("tenant_id"),)


class BudgetReservation(Base, TenantScoped):
    """Spend reserved before a model call and reconciled after it.

    reserved -> incurred (actual cost known) -> settled (in the ledger) | released (call never billed)
    """

    __tablename__ = "budget_reservations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(String(36), index=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    estimate_usd: Mapped[Decimal] = mapped_column(Money)
    actual_usd: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    model: Mapped[str | None] = mapped_column(String(60), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12), default="reserved", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Task(Base, TenantScoped):
    """A durable unit of work processed by the worker (queue + workflow state)."""

    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(60), index=True)  # agent_run | workflow step name
    idempotency_key: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # queued -> running -> succeeded | failed(retry) | dead | waiting_approval
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Changes on every claim; a worker may only commit results while it still holds the token.
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ApprovalRequest(Base, TenantScoped):
    """An action that requires owner authority before it can execute."""

    __tablename__ = "approval_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    category: Mapped[str] = mapped_column(String(60), index=True)
    action: Mapped[str] = mapped_column(String(80))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    requested_by: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending|approved|rejected|executed
    decided_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base, TenantScoped):
    __tablename__ = "audit_log"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(80), index=True)
    target: Mapped[str] = mapped_column(String(120), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


# ---------------------------------------------------------------- CRM


class Company(Base, TenantScoped):
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    industry: Mapped[str] = mapped_column(String(120), default="")
    website: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Contact(Base, TenantScoped):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("tenant_id", "email"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100), default="")
    email: Mapped[str] = mapped_column(String(254))
    phone: Mapped[str] = mapped_column(String(40), default="")
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    source: Mapped[str] = mapped_column(String(60), default="manual")
    lifecycle: Mapped[str] = mapped_column(String(20), default="lead")  # lead|qualified|customer|disqualified
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Deal(Base, TenantScoped):
    __tablename__ = "deals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200))
    contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id"))
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    stage: Mapped[str] = mapped_column(String(30), default="lead", index=True)
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    owner_agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Activity(Base, TenantScoped):
    __tablename__ = "activities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    deal_id: Mapped[str | None] = mapped_column(ForeignKey("deals.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))  # note|email|call|meeting|stage_change|document
    summary: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(120), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------- Documents & comms


class Document(Base, TenantScoped):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(40))  # proposal|sow|invoice|status_report
    title: Mapped[str] = mapped_column(String(200))
    deal_id: Mapped[str | None] = mapped_column(ForeignKey("deals.id"), nullable=True)
    html: Mapped[str] = mapped_column(Text)
    markdown: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(120), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OutboundMessage(Base, TenantScoped):
    """Transactional outbox: every external message is persisted before sending."""

    __tablename__ = "outbound_messages"
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    channel: Mapped[str] = mapped_column(String(20), default="email")
    to_address: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    # draft -> pending_approval -> queued -> sending -> sent | failed | rejected | unknown
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
