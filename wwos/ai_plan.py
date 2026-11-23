"""Low-budget AI plan: the right model for each department and a budget split that fits a small cap.

The owner applies it with ``wwos ai-plan --tenant <name> --budget 20``. It is an owner action (audited):

* sets the organisation's monthly AI budget;
* moves every founding agent to its tier's model and its share of the budget, as a new tested
  version (``lifecycle.revise`` with the owner as actor), so the acceptance-test rule still holds.

Tiers (prices per million input/output tokens, ``budget.MODEL_PRICES``):

* **premium** - Claude Opus 5.5 ($4/$20): judgement and anything a client or partner reads
  (CEO planning, proposals and correspondence, statements of work, agreements).
* **standard** - Claude Sonnet 5.5 ($2/$10): internal analysis (agent workforce, QA review, campaigns).
* **economy** - Claude Haiku 5.5 ($0.10/$0.50, low effort): routine checks (operations, invoicing).

Budget shares leave about a quarter of the budget unallocated for specialists the CEO creates.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from .agents import lifecycle
from .budget import price
from .models import Agent, Tenant
from .policy import audit

TIER_MODELS = {
    "premium": "claude-opus-5-5",
    "standard": "claude-sonnet-5-5",
    "economy": "claude-haiku-5-5",
}

# role -> (tier, share of the organisation budget in percent)
ROLE_PLAN: dict[str, tuple[str, Decimal]] = {
    "ceo": ("premium", Decimal("20")),
    "sales": ("premium", Decimal("18")),
    "delivery": ("premium", Decimal("10")),
    "legal": ("premium", Decimal("4")),
    "technology": ("standard", Decimal("6")),
    "qa": ("standard", Decimal("6")),
    "marketing": ("standard", Decimal("5")),
    "finance": ("economy", Decimal("2")),
    "operations": ("economy", Decimal("3")),
}
SPECIALIST_MODEL = TIER_MODELS["standard"]

# Typical agent run (measured prompt sizes are 500-1,900 tokens; a run averages ~4 model turns):
# ~12,000 input tokens of which ~60% are cache reads after the first turn, ~3,000 output tokens.
TYPICAL_RUN = {"input": 4_800, "cache_write": 2_400, "cache_read": 7_200, "output": 3_000}


@dataclass
class PlanLine:
    role: str
    name: str
    tier: str
    model: str
    budget: Decimal
    est_run_usd: Decimal
    status: str = ""

    @property
    def runs_per_month(self) -> int:
        return int(self.budget / self.est_run_usd) if self.est_run_usd else 0


def typical_run_cost(model: str) -> Decimal:
    pin, pout = price(model)
    t = TYPICAL_RUN
    inp = Decimal(t["input"]) + Decimal("1.25") * t["cache_write"] + Decimal("0.1") * t["cache_read"]
    return ((pin * inp + pout * t["output"]) / Decimal(1_000_000)).quantize(Decimal("0.0001"))


def plan_lines(monthly_budget: Decimal) -> list[PlanLine]:
    from .bootstrap import FOUNDING_AGENTS

    lines = []
    for spec in FOUNDING_AGENTS:
        tier, pct = ROLE_PLAN[spec["role"]]
        model = TIER_MODELS[tier]
        share = (monthly_budget * pct / Decimal(100)).quantize(Decimal("0.01"))
        lines.append(PlanLine(spec["role"], spec["name"], tier, model, share, typical_run_cost(model)))
    return lines


def apply(s, tenant: Tenant, monthly_budget: Decimal) -> list[PlanLine]:
    """Owner action. Raises ValueError for an invalid budget; agent failures are reported per line."""
    from .config import get_settings

    monthly_budget = Decimal(str(monthly_budget))
    if monthly_budget < 0:
        raise ValueError("budget cannot be negative")
    hard = get_settings().hard_monthly_cap_usd
    if hard is not None and monthly_budget > hard:
        raise ValueError(f"${monthly_budget} exceeds the installation hard cap of ${hard}/month "
                         "(WWOS_HARD_MONTHLY_CAP_USD)")
    lines = plan_lines(monthly_budget)
    cap = get_settings().max_agent_budget_usd
    if any(line.budget > cap for line in lines):
        raise ValueError(f"an agent share would exceed the per-agent ceiling ${cap}")
    old = tenant.monthly_budget_usd
    # While agents are revised one by one, old and new allocations coexist, so check against the larger
    # budget and set the final value afterwards (checked below). revise() excludes the replaced agent.
    tenant.monthly_budget_usd = max(monthly_budget, old)
    s.flush()
    for line in lines:
        agent = s.scalars(select(Agent).where(Agent.role == line.role, Agent.status == "active")
                          .order_by(Agent.version.desc())).first()
        if agent is None:
            line.status = "no active agent with this role (skipped)"
            continue
        if agent.model == line.model and agent.monthly_budget_usd == line.budget:
            line.status = "unchanged"
            continue
        new = lifecycle.revise(s, tenant, agent, actor_agent=None, model=line.model,
                               monthly_budget_usd=line.budget)
        line.status = "updated" if new.status == "active" else f"not changed: {new.status_reason}"
    from .budget import allocated_to_agents

    allocated = allocated_to_agents(s)
    if allocated > monthly_budget:
        raise ValueError(f"agents still hold ${allocated} of budget, more than ${monthly_budget}: retire or "
                         "reduce specialist agents first (nothing was changed)")
    tenant.monthly_budget_usd = monthly_budget
    s.flush()
    audit(s, "owner", "ai_plan.applied", tenant.id, budget=str(monthly_budget), previous_budget=str(old),
          agents={line.role: [line.model, str(line.budget), line.status] for line in lines})
    return lines
