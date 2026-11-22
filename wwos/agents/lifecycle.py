"""Agent lifecycle: provision, test, activate, revise (versioning), suspend, replace, retire.

Safety rules enforced here (not left to prompts):

* **Privilege containment** - an agent can only grant tools it holds itself and
  can never create another provisioning-capable agent. Only the owner can.
* **Budget containment** - the sum of agent budgets may not exceed the tenant's
  monthly budget, and a single agent may not exceed WWOS_MAX_AGENT_BUDGET_USD.
* **Headcount ceiling** - WWOS_MAX_AGENTS_PER_TENANT live agents.
* **Test before activate** - every new agent or version must pass an automated
  acceptance check before it can act; a failing version never replaces a
  working one.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..budget import ALLOWED_MODELS, allocated_to_agents
from ..config import get_settings
from ..models import Agent, Department, Tenant
from ..policy import PROVISIONING_TOOLS, audit

LIVE_STATUSES = ("draft", "testing", "active", "suspended")
OWNER = "owner"


class ProvisioningError(ValueError):
    pass


def _known_tools() -> set[str]:
    from ..tools import TOOLS

    return set(TOOLS)


def _live_count(s: Session) -> int:
    return s.execute(select(func.count(Agent.id)).where(Agent.status.in_(LIVE_STATUSES))).scalar_one()


def validate_spec(s: Session, tenant: Tenant, *, creator: Agent | None, tools: list[str], model: str,
                  monthly_budget_usd: Decimal, can_provision: bool, instructions: str,
                  replacing_agent_id: str | None = None, baseline_tools: list[str] | None = None) -> None:
    cfg = get_settings()
    if model not in ALLOWED_MODELS or model == "offline":
        raise ProvisioningError(f"model {model!r} is not on the approved list")
    unknown = set(tools) - _known_tools()
    if unknown:
        raise ProvisioningError(f"unknown tools: {sorted(unknown)}")
    if not instructions or len(instructions.strip()) < 40:
        raise ProvisioningError("instructions must describe the agent's job (min 40 characters)")
    if monthly_budget_usd <= 0:
        raise ProvisioningError("monthly budget must be positive")
    if monthly_budget_usd > cfg.max_agent_budget_usd:
        raise ProvisioningError(f"agent budget exceeds the per-agent ceiling ${cfg.max_agent_budget_usd}")
    allocated = allocated_to_agents(s, exclude_agent_id=replacing_agent_id)
    if allocated + monthly_budget_usd > tenant.monthly_budget_usd:
        raise ProvisioningError(
            f"allocating ${monthly_budget_usd} would exceed the organisation budget "
            f"(${allocated} of ${tenant.monthly_budget_usd} already allocated)"
        )
    if replacing_agent_id is None and _live_count(s) >= cfg.max_agents_per_tenant:
        raise ProvisioningError(f"agent ceiling of {cfg.max_agents_per_tenant} reached")
    if creator is not None:
        # A creator may grant tools it holds, and keep (never add to) an existing agent's tools.
        extra = set(tools) - set(creator.tools or []) - set(baseline_tools or [])
        if extra:
            raise ProvisioningError(f"{creator.name} cannot grant tools it does not hold: {sorted(extra)}")
        if can_provision or set(tools) & PROVISIONING_TOOLS:
            raise ProvisioningError("only the owner can create agents that manage other agents")


def acceptance_test(s: Session, tenant: Tenant, agent: Agent) -> tuple[bool, str]:
    """Automated acceptance check run before any agent version goes live.

    1. Static checks: tools resolvable, model approved, budget positive.
    2. Permission check: a tool the agent does NOT hold must be refused.
    3. Live smoke test: the agent answers a short prompt through the real runtime
       (counts against its budget, capped at a small max_tokens).
    """
    from ..policy import PermissionDenied, check_tool_permission
    from .runtime import RunTrace, persist_spend, run_agent_prompt

    if set(agent.tools or []) - _known_tools():
        return False, "unresolvable tools"
    probe = next((t for t in sorted(_known_tools()) if t not in (agent.tools or [])), None)
    if probe:
        try:
            check_tool_permission(agent, probe)
            return False, f"permission leak: {probe} was not refused"
        except PermissionDenied:
            pass
    trace = RunTrace()
    try:
        result = run_agent_prompt(
            s, tenant, agent,
            "Acceptance check: in one sentence, state your role and the first thing you would do.",
            max_tokens=400, allow_tools=False, trace=trace,
        )
    except Exception as e:  # noqa: BLE001 - any failure fails the test
        return False, f"smoke test failed: {e}"
    finally:
        persist_spend(s, trace)
    if not result.strip():
        return False, "smoke test returned no text"
    return True, "passed"


def provision(s: Session, tenant: Tenant, *, name: str, role: str, department_code: str, instructions: str,
              tools: list[str], model: str | None = None, monthly_budget_usd: Decimal | str | float = "10",
              can_provision: bool = False, creator: Agent | None = None, lineage_id: str | None = None,
              version: int = 1, replacing_agent_id: str | None = None, activate: bool = True,
              baseline_tools: list[str] | None = None) -> Agent:
    model = model or get_settings().default_model
    budget = Decimal(str(monthly_budget_usd))
    validate_spec(s, tenant, creator=creator, tools=tools, model=model, monthly_budget_usd=budget,
                  can_provision=can_provision, instructions=instructions, replacing_agent_id=replacing_agent_id,
                  baseline_tools=baseline_tools)
    dept = s.scalars(select(Department).where(Department.code == department_code)).first()
    if dept is None:
        raise ProvisioningError(f"unknown department {department_code!r}")
    actor = creator.name if creator else OWNER
    agent = Agent(name=name.strip(), role=role, department_id=dept.id, instructions=instructions.strip(),
                  model=model, tools=sorted(set(tools)), monthly_budget_usd=budget,
                  can_provision_agents=can_provision, created_by=actor, status="testing", version=version)
    if lineage_id:
        agent.lineage_id = lineage_id
    s.add(agent)
    s.flush()
    audit(s, actor, "agent.provisioned", agent.id, name=agent.name, version=version, tools=agent.tools,
          budget=str(budget), model=model)
    if not activate:
        return agent
    ok, detail = acceptance_test(s, tenant, agent)
    if ok:
        agent.status = "active"
        agent.status_reason = "acceptance test passed"
    else:
        agent.status = "retired"
        agent.status_reason = f"acceptance test failed: {detail}"
    audit(s, "qa", "agent.acceptance_test", agent.id, passed=ok, detail=detail)
    return agent


def suspend(s: Session, agent: Agent, reason: str, actor: str) -> Agent:
    if agent.status in ("retired",):
        raise ProvisioningError("agent is retired")
    if agent.can_provision_agents and actor != OWNER:
        raise ProvisioningError("only the owner can suspend a provisioning-capable agent")
    agent.status = "suspended"
    agent.status_reason = reason
    audit(s, actor, "agent.suspended", agent.id, reason=reason)
    return agent


def reactivate(s: Session, tenant: Tenant, agent: Agent, actor: str) -> Agent:
    if agent.status != "suspended":
        raise ProvisioningError("only suspended agents can be reactivated")
    agent.status = "testing"  # the runtime refuses suspended agents, so test in the testing state
    ok, detail = acceptance_test(s, tenant, agent)
    if not ok:
        agent.status = "suspended"
        raise ProvisioningError(f"reactivation blocked: {detail}")
    agent.status = "active"
    agent.consecutive_failures = 0
    agent.status_reason = f"reactivated by {actor}"
    audit(s, actor, "agent.reactivated", agent.id)
    return agent


def revise(s: Session, tenant: Tenant, agent: Agent, *, actor_agent: Agent | None,
           instructions: str | None = None, tools: list[str] | None = None, model: str | None = None,
           monthly_budget_usd: Decimal | str | None = None) -> Agent:
    """Create and test a new version; swap it in only if it passes."""
    if agent.status == "retired":
        raise ProvisioningError("cannot revise a retired agent")
    if agent.can_provision_agents and actor_agent is not None:
        raise ProvisioningError("only the owner can revise a provisioning-capable agent")
    dept = s.get(Department, agent.department_id)
    new = provision(
        s, tenant, name=agent.name, role=agent.role, department_code=dept.code,
        instructions=instructions or agent.instructions, tools=tools if tools is not None else list(agent.tools),
        model=model or agent.model,
        monthly_budget_usd=monthly_budget_usd if monthly_budget_usd is not None else agent.monthly_budget_usd,
        can_provision=agent.can_provision_agents, creator=actor_agent, lineage_id=agent.lineage_id,
        version=agent.version + 1, replacing_agent_id=agent.id, baseline_tools=list(agent.tools or []),
    )
    if new.status == "active":
        agent.status = "retired"
        agent.status_reason = f"superseded by version {new.version}"
        audit(s, actor_agent.name if actor_agent else OWNER, "agent.revised", agent.id, new_version=new.version)
    return new


def replace_failed(s: Session, tenant: Tenant, agent: Agent, actor_agent: Agent | None) -> Agent:
    """Replace a suspended/failed agent with a fresh tested instance of the same spec."""
    if agent.status not in ("suspended",):
        raise ProvisioningError("only suspended agents can be replaced")
    return revise(s, tenant, agent, actor_agent=actor_agent)


def tenant_of(s: Session) -> Tenant:
    tenant_id = s.info.get("tenant_id")
    tenant = s.get(Tenant, tenant_id)
    if tenant is None:
        raise ProvisioningError("tenant not found")
    return tenant
