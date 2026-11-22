from decimal import Decimal

import pytest
from sqlalchemy import select

from wwos import budget
from wwos.agents import lifecycle
from wwos.agents.runtime import RunTrace, execute_tool, find_agent, persist_spend, run_agent_prompt
from wwos.budget import BudgetExceeded
from wwos.db import tenant_session
from wwos.models import Agent, ApprovalRequest, CostEntry, Tenant
from wwos.policy import PermissionDenied
from wwos.tools import ToolContext

INSTR = "You research market segments for consulting offers and summarise findings for the CEO."


def _ctx(s, tid, role):
    return ToolContext(session=s, tenant=s.get(Tenant, tid), agent=find_agent(s, role=role))


def test_cost_calculation():
    assert budget.cost_of("claude-opus-5-5", 1_000_000, 1_000_000) == Decimal("24")
    assert budget.cost_of("claude-haiku-5-5", 2_000_000, 0) == Decimal("0.2")
    with pytest.raises(ValueError):
        budget.cost_of("gpt-unknown", 1, 1)


def test_tool_permissions_enforced(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        ctx = _ctx(s, tid, "marketing")
        out, err = execute_tool(ctx, "finance.create_invoice", {})
        assert err and "permission denied" in out["error"]
        out, err = execute_tool(ctx, "agents.provision", {})
        assert err and "permission denied" in out["error"]


def test_owner_reserved_action_becomes_approval_and_does_not_execute(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        ctx = _ctx(s, tid, "finance")
        out, err = execute_tool(ctx, "finance.transfer_funds", {"amount": "5000", "currency": "USD", "payee": "X"})
        assert not err and out["status"] == "pending_owner_approval"
        req = s.get(ApprovalRequest, out["approval_id"])
        assert req.category == "financial_transfer" and req.status == "pending"
        # The CEO also cannot raise the budget directly.
        ceo = _ctx(s, tid, "ceo")
        out, _ = execute_tool(ceo, "budget.request_increase", {"new_monthly_budget_usd": "5000", "justification": "x"})
        assert out["status"] == "pending_owner_approval"
        assert s.get(Tenant, tid).monthly_budget_usd == Decimal("500")


def test_suspended_agent_cannot_act(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        sales = find_agent(s, role="sales")
        lifecycle.suspend(s, sales, "test", "owner")
        out, err = execute_tool(ToolContext(s, s.get(Tenant, tid), sales), "crm.pipeline_summary", {})
        assert err
        with pytest.raises(PermissionDenied):
            run_agent_prompt(s, s.get(Tenant, tid), sales, "hello")


def test_agent_budget_blocks_spend(tenant, scripted):
    tid, _ = tenant
    # Output at the max_tokens cap: $2.00 per call on Opus 5.5 (+ tiny input).
    scripted(input_tokens=10, output_tokens=100_000)
    with tenant_session(tid) as s:
        qa = find_agent(s, role="qa")
        qa.monthly_budget_usd = Decimal("5")
        qa_id = qa.id
    with tenant_session(tid) as s:
        tenant_obj = s.get(Tenant, tid)
        qa = s.get(Agent, qa_id)
        trace = RunTrace()
        run_agent_prompt(s, tenant_obj, qa, "one", max_tokens=100_000, trace=trace)
        run_agent_prompt(s, tenant_obj, qa, "two", max_tokens=100_000, trace=trace)
        # In-flight (not yet committed) spend counts: the third call is refused inside the same run.
        with pytest.raises(BudgetExceeded):
            run_agent_prompt(s, tenant_obj, qa, "three", max_tokens=100_000, trace=trace)
    with tenant_session(tid) as s:  # ledger written when the session ended
        assert budget.spent_this_month(s, qa_id) == Decimal("4.00008")
        with pytest.raises(BudgetExceeded):
            run_agent_prompt(s, s.get(Tenant, tid), s.get(Agent, qa_id), "four", max_tokens=100_000)


def test_org_budget_blocks_spend(tenant, scripted):
    tid, _ = tenant
    scripted(input_tokens=10, output_tokens=10)
    with tenant_session(tid) as s:
        s.add(CostEntry(model="claude-opus-5-5", cost_usd=Decimal("499.99"), input_tokens=0, output_tokens=0))
    with tenant_session(tid) as s:
        with pytest.raises(BudgetExceeded, match="organisation"):
            run_agent_prompt(s, s.get(Tenant, tid), find_agent(s, role="sales"), "hi", max_tokens=2000)


def test_privilege_containment_for_agent_created_agents(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        t = s.get(Tenant, tid)
        ceo = find_agent(s, role="ceo")
        with pytest.raises(lifecycle.ProvisioningError, match="does not hold"):
            lifecycle.provision(s, t, name="Sneaky", role="specialist", department_code="sales", instructions=INSTR,
                                tools=["finance.transfer_funds"], creator=ceo)
        with pytest.raises(lifecycle.ProvisioningError, match="only the owner"):
            lifecycle.provision(s, t, name="Sub-CEO", role="specialist", department_code="sales",
                                instructions=INSTR, tools=["agents.provision"], creator=ceo)
        with pytest.raises(lifecycle.ProvisioningError, match="only the owner"):
            lifecycle.provision(s, t, name="Sub-CEO", role="specialist", department_code="sales",
                                instructions=INSTR, tools=["deals.get"], creator=ceo, can_provision=True)
        ok = lifecycle.provision(s, t, name="Researcher", role="specialist", department_code="sales",
                                 instructions=INSTR, tools=["deals.get"], creator=ceo, monthly_budget_usd="5")
        assert ok.status == "active" and ok.created_by == "CEO"


def test_budget_and_headcount_ceilings(tenant, monkeypatch):
    tid, _ = tenant
    with tenant_session(tid) as s:
        t = s.get(Tenant, tid)
        with pytest.raises(lifecycle.ProvisioningError, match="per-agent ceiling"):
            lifecycle.provision(s, t, name="Big", role="x", department_code="sales", instructions=INSTR,
                                tools=[], monthly_budget_usd="101")
        # 380 of 500 allocated by founding agents; 121 more breaches the org budget.
        with pytest.raises(lifecycle.ProvisioningError, match="organisation budget"):
            lifecycle.provision(s, t, name="A", role="x", department_code="sales", instructions=INSTR, tools=[],
                                monthly_budget_usd="100")
            lifecycle.provision(s, t, name="B", role="x", department_code="sales", instructions=INSTR, tools=[],
                                monthly_budget_usd="21")
        monkeypatch.setenv("WWOS_MAX_AGENTS_PER_TENANT", "10")
        with pytest.raises(lifecycle.ProvisioningError, match="ceiling"):
            lifecycle.provision(s, t, name="C", role="x", department_code="sales", instructions=INSTR, tools=[],
                                monthly_budget_usd="1")


def test_failed_acceptance_test_retires_agent(tenant, scripted):
    tid, _ = tenant
    scripted(script=[[{"type": "text", "text": ""}]])  # empty reply fails the smoke test
    with tenant_session(tid) as s:
        a = lifecycle.provision(s, s.get(Tenant, tid), name="Mute", role="x", department_code="sales",
                                instructions=INSTR, tools=[], monthly_budget_usd="1")
        assert a.status == "retired" and "acceptance test failed" in a.status_reason


def test_revision_versioning_and_safe_rollout(tenant, scripted):
    tid, _ = tenant
    with tenant_session(tid) as s:
        t = s.get(Tenant, tid)
        sales = find_agent(s, role="sales")
        v2 = lifecycle.revise(s, t, sales, actor_agent=find_agent(s, role="technology"),
                              instructions=INSTR + " Also keep notes concise.")
        assert v2.version == 2 and v2.status == "active" and v2.lineage_id == sales.lineage_id
        assert sales.status == "retired"
        # A revision that fails acceptance must not take down the working version.
        scripted(script=[[{"type": "text", "text": ""}]])
        v3 = lifecycle.revise(s, t, v2, actor_agent=None, instructions=INSTR + " Broken revision.")
        assert v3.status == "retired" and v2.status == "active"


def test_owner_only_controls_provisioning_agents(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        ceo = find_agent(s, role="ceo")
        with pytest.raises(lifecycle.ProvisioningError):
            lifecycle.suspend(s, ceo, "coup", "Technology Director")
        with pytest.raises(lifecycle.ProvisioningError):
            lifecycle.revise(s, s.get(Tenant, tid), ceo, actor_agent=find_agent(s, role="technology"))
        assert s.scalars(select(Agent).where(Agent.role == "ceo")).one().status == "active"
