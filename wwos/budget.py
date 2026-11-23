"""Budget enforcement and cost accounting.

Spend is checked *before* every model call against three ceilings:

* the agent's own monthly budget,
* the tenant's monthly budget,
* a worst-case estimate of the call (``max_tokens`` at output price).

Actual spend is recorded in the immutable ``cost_entries`` ledger afterwards.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Agent, CostEntry, Tenant

# USD per million tokens (input, output). Source: Anthropic pricing, cached 2026-11-03.
MODEL_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-fable-5-2": (Decimal("8"), Decimal("40")),
    "claude-opus-5-5": (Decimal("4"), Decimal("20")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
    "offline": (Decimal("0"), Decimal("0")),
}
ALLOWED_MODELS = set(MODEL_PRICES)


class BudgetExceeded(RuntimeError):
    pass


class PaceDeferred(BudgetExceeded):
    """Spending pacing (WWOS_BUDGET_PACING_BURST_PCT): the monthly budget is not used up, but today's
    share of it is. The work is postponed to ``retry_at`` (next UTC midnight), not failed."""

    def __init__(self, message: str, retry_at: datetime) -> None:
        super().__init__(message)
        self.retry_at = retry_at


def pace_limit(monthly_budget: Decimal, now: datetime | None = None) -> Decimal | None:
    """Cumulative spend allowed so far this month under pacing, or None when pacing is off.

    allowed = budget x (day of month / days in month) + burst, capped at the budget. Unused allowance
    carries forward, and the burst lets the owner run a larger job on any day of the month."""
    import calendar

    from .config import get_settings

    pct = get_settings().budget_pacing_burst_pct
    if pct is None:
        return None
    now = now or datetime.now(timezone.utc)
    days = calendar.monthrange(now.year, now.month)[1]
    allowed = monthly_budget * Decimal(now.day) / Decimal(days) + monthly_budget * pct / Decimal(100)
    return min(monthly_budget, allowed.quantize(Decimal("0.0001")))


def next_day_start(now: datetime | None = None) -> datetime:
    from datetime import timedelta

    now = now or datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def price(model: str) -> tuple[Decimal, Decimal]:
    if model not in MODEL_PRICES:
        raise ValueError(f"unknown or unapproved model: {model}")
    return MODEL_PRICES[model]


# Prompt caching (5-minute entries): writes bill at 1.25x the input price, reads at 0.1x.
CACHE_WRITE_MULTIPLIER = Decimal("1.25")
CACHE_READ_MULTIPLIER = Decimal("0.1")


def cost_of(model: str, input_tokens: int, output_tokens: int, cache_write_tokens: int = 0,
            cache_read_tokens: int = 0) -> Decimal:
    """``input_tokens`` are the uncached input tokens (as the API reports them); cache tokens are extra."""
    pin, pout = price(model)
    inp = (Decimal(input_tokens) + CACHE_WRITE_MULTIPLIER * cache_write_tokens
           + CACHE_READ_MULTIPLIER * cache_read_tokens)
    return (pin * inp + pout * output_tokens) / Decimal(1_000_000)


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def spent_this_month(session: Session, agent_id: str | None = None) -> Decimal:
    q = select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(CostEntry.created_at >= month_start())
    if agent_id:
        q = q.where(CostEntry.agent_id == agent_id)
    return Decimal(str(session.execute(q).scalar_one()))


def allocated_to_agents(session: Session, exclude_agent_id: str | None = None) -> Decimal:
    q = select(func.coalesce(func.sum(Agent.monthly_budget_usd), 0)).where(
        Agent.status.in_(["draft", "testing", "active", "suspended"])
    )
    if exclude_agent_id:
        q = q.where(Agent.id != exclude_agent_id)
    return Decimal(str(session.execute(q).scalar_one()))


def ensure_can_spend(session: Session, tenant: Tenant, agent: Agent, estimate_usd: Decimal,
                     agent_pending: Decimal = Decimal("0"), org_pending: Decimal = Decimal("0")) -> None:
    """``*_pending`` is spend already incurred in the current run but not yet in the ledger."""
    agent_spent = spent_this_month(session, agent.id) + agent_pending
    if agent_spent + estimate_usd > agent.monthly_budget_usd:
        raise BudgetExceeded(
            f"agent {agent.name} budget ${agent.monthly_budget_usd} would be exceeded "
            f"(spent ${agent_spent:.4f}, call estimate ${estimate_usd:.4f})"
        )
    org_spent = spent_this_month(session) + org_pending
    if org_spent + estimate_usd > tenant.monthly_budget_usd:
        raise BudgetExceeded(
            f"organisation budget ${tenant.monthly_budget_usd} would be exceeded "
            f"(spent ${org_spent:.4f}, call estimate ${estimate_usd:.4f})"
        )


def worst_case_estimate(model: str, prompt_chars: int, max_tokens: int) -> Decimal:
    # Real English text is ~4 chars/token; chars/3 also covers the whole prompt being a cache write
    # (1.25 x chars/4 < chars/3). Output is priced at the cap.
    return cost_of(model, prompt_chars // 3 + 1, max_tokens)


def record(session: Session, *, agent_id: str | None, task_id: str | None, model: str,
           input_tokens: int, output_tokens: int, reservation_id: str | None = None) -> CostEntry:
    entry = CostEntry(
        reservation_id=reservation_id,
        agent_id=agent_id,
        task_id=task_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_of(model, input_tokens, output_tokens),
    )
    session.add(entry)
    return entry


def pace_status(session: Session, tenant: Tenant) -> dict:
    """Spend so far vs the pacing allowance (pacing off: the whole monthly budget is available)."""
    spent = spent_this_month(session)
    allowed = pace_limit(tenant.monthly_budget_usd)
    limit = tenant.monthly_budget_usd if allowed is None else allowed
    return {"pacing": allowed is not None, "usable_so_far": limit,
            "available_now": max(limit - spent, Decimal("0")).quantize(Decimal("0.0001"))}
