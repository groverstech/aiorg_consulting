"""The $20/month plan: tiered models, prompt caching, spending pacing, an installation hard cap and weekly
AI reviews. No real model is called (scripted providers and a mock HTTP transport)."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import anthropic
import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from wwos import ai_plan, budget, budget_guard, llm, schedule, workflows
from wwos.agents.runtime import ToolContext, execute_tool
from wwos.api import app
from wwos.budget import BudgetExceeded, PaceDeferred
from wwos.db import tenant_session
from wwos.llm import AnthropicProvider, LLMResult
from wwos.models import Agent, AuditLog, CostEntry, Task, Tenant

pytestmark = pytest.mark.acceptance
ROOT = Path(__file__).resolve().parents[2]


def _zero_tenant(monkeypatch):
    monkeypatch.setenv("WWOS_LLM_PROVIDER", "none")
    from wwos.bootstrap import create_tenant

    return create_tenant("White Waters Consulting Group", "0")


def _active(s):
    return {a.role: a for a in s.scalars(select(Agent).where(Agent.status == "active"))}


def test_ai_plan_gives_each_department_the_right_model_and_fits_20_dollars(monkeypatch):
    tid, _ = _zero_tenant(monkeypatch)
    with tenant_session(tid) as s:
        lines = ai_plan.apply(s, s.get(Tenant, tid), Decimal("20"))
    assert all(line.status == "updated" for line in lines), [(line.role, line.status) for line in lines]
    with tenant_session(tid) as s:
        agents = _active(s)
        assert s.get(Tenant, tid).monthly_budget_usd == Decimal("20")
        assert {r: agents[r].model for r in ("ceo", "sales", "delivery", "legal")} == dict.fromkeys(
            ("ceo", "sales", "delivery", "legal"), "claude-opus-5-5")
        assert agents["qa"].model == agents["technology"].model == "claude-sonnet-5-5"
        assert agents["operations"].model == agents["finance"].model == "claude-haiku-5-5"
        assert all(a.version == 2 for a in agents.values())  # each change is a new, tested version
        assert sum(a.monthly_budget_usd for a in agents.values()) == Decimal("14.80")  # $5.20 left for specialists
        assert budget.allocated_to_agents(s) <= Decimal("20")
        assert s.scalars(select(AuditLog).where(AuditLog.action == "ai_plan.applied")).first() is not None
    with tenant_session(tid) as s:  # re-applying is a no-op
        assert {line.status for line in ai_plan.apply(s, s.get(Tenant, tid), Decimal("20"))} == {"unchanged"}


def test_plan_estimates_show_capacity():
    lines = {line.role: line for line in ai_plan.plan_lines(Decimal("20"))}
    assert lines["ceo"].est_run_usd < Decimal("0.1") and lines["ceo"].runs_per_month >= 40
    assert lines["operations"].est_run_usd < Decimal("0.005")  # economy tier is ~40x cheaper per run


def test_hard_cap_refuses_a_bigger_plan_and_approved_increases(monkeypatch, tenant):
    monkeypatch.setenv("WWOS_HARD_MONTHLY_CAP_USD", "20")
    tid, _ = tenant
    with tenant_session(tid) as s:
        with pytest.raises(ValueError, match="hard cap"):
            ai_plan.apply(s, s.get(Tenant, tid), Decimal("25"))
    with tenant_session(tid) as s:
        t = s.get(Tenant, tid)
        before = t.monthly_budget_usd
        ctx = ToolContext(session=s, tenant=t, agent=None, task_id=None)  # the owner executing an approval
        out, is_err = execute_tool(ctx, "budget.request_increase",
                                   {"new_monthly_budget_usd": 900, "justification": "growth"})
        assert is_err and "hard cap" in out["error"]
        assert s.get(Tenant, tid).monthly_budget_usd == before


def test_hard_cap_holds_across_every_tenant(monkeypatch, tenant, other_tenant):
    monkeypatch.setenv("WWOS_HARD_MONTHLY_CAP_USD", "1.00")
    store = budget_guard.store()
    rids = []
    for tid, _ in (tenant, other_tenant):
        with tenant_session(tid) as s:
            a = _active(s)["ceo"]
            rids.append(store.reserve(tenant_id=tid, agent_id=a.id, agent_name=a.name,
                                      agent_budget=a.monthly_budget_usd, task_id=None, estimate=Decimal("0.45")))
    with tenant_session(other_tenant[0]) as s:
        a = _active(s)["sales"]
        with pytest.raises(BudgetExceeded, match="installation hard cap"):
            store.reserve(tenant_id=other_tenant[0], agent_id=a.id, agent_name=a.name,
                          agent_budget=a.monthly_budget_usd, task_id=None, estimate=Decimal("0.20"))
    for r in rids:
        store.release(r)


def test_cache_tokens_are_priced_and_recorded(scripted, tenant):
    # 1,000 uncached + 2,000 written (x1.25) + 10,000 read (x0.1) input tokens and 100 output on Opus 5.5
    assert budget.cost_of("claude-opus-5-5", 1000, 100, 2000, 10000) == Decimal("0.02")

    class Cached:
        name = "cached"

        def complete(self, **kw):
            return LLMResult(content=[{"type": "text", "text": "ok"}], stop_reason="end_turn", input_tokens=1000,
                             output_tokens=100, model="claude-opus-5-5", cache_write_tokens=2000,
                             cache_read_tokens=10000)
    llm.set_provider(Cached())
    tid, _ = tenant
    with tenant_session(tid) as s:
        workflows.enqueue(s, kind="agent_run", agent_id=_active(s)["ceo"].id, idempotency_key="c1",
                          payload={"instruction": "Say hello"})
    workflows.drain()
    with tenant_session(tid) as s:
        row = s.scalars(select(CostEntry).order_by(CostEntry.created_at.desc())).first()
        assert row.cost_usd == Decimal("0.02") and row.input_tokens == 13000 and row.output_tokens == 100


def _client(handler):
    return AnthropicProvider(client=anthropic.Anthropic(
        api_key="test-key", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler))))


def test_provider_uses_prompt_caching_and_low_effort_for_the_economy_model():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 50, "output_tokens": 5, "cache_creation_input_tokens": 700,
                      "cache_read_input_tokens": 1400}})
    p = _client(handler)
    r = p.complete(model="claude-haiku-5-5", system="s", messages=[{"role": "user", "content": "x"}], tools=[],
                   max_tokens=100)
    p.complete(model="claude-opus-5-5", system="s", messages=[{"role": "user", "content": "x"}], tools=[],
               max_tokens=100)
    assert seen[0]["cache_control"] == {"type": "ephemeral"} and seen[1]["cache_control"] == {"type": "ephemeral"}
    assert seen[0]["output_config"] == {"effort": "low"} and seen[1]["output_config"] == {"effort": "medium"}
    assert (r.input_tokens, r.cache_write_tokens, r.cache_read_tokens) == (50, 700, 1400)


def _freeze(monkeypatch, day: int) -> datetime:
    now = datetime.now(timezone.utc)
    frozen = now.replace(day=day, hour=12, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(budget_guard, "utcnow", lambda: frozen)
    return frozen


def test_pacing_spreads_the_budget_and_defers_to_tomorrow(monkeypatch):
    monkeypatch.setenv("WWOS_BUDGET_PACING_BURST_PCT", "25")
    tid, _ = _zero_tenant(monkeypatch)
    with tenant_session(tid) as s:
        ai_plan.apply(s, s.get(Tenant, tid), Decimal("20"))
        pair = [dict(agent_id=a.id, agent_name=a.name, agent_budget=a.monthly_budget_usd)
                for a in (_active(s)["ceo"], _active(s)["sales"])]  # $4.00 and $3.60: agent limits not binding
    frozen = _freeze(monkeypatch, 1)
    days = __import__("calendar").monthrange(frozen.year, frozen.month)[1]
    limit = budget.pace_limit(Decimal("20"), frozen)
    assert limit == (Decimal("20") / days + Decimal("5")).quantize(Decimal("0.0001"))  # day 1: 1/days + 25% burst
    store = budget_guard.store()
    held = [store.reserve(tenant_id=tid, task_id=None, estimate=Decimal("1.5"), **pair[i % 2]) for i in range(3)]
    with pytest.raises(PaceDeferred) as e:  # $6.00 would pass day 1's ~$5.67 allowance
        store.reserve(tenant_id=tid, task_id=None, estimate=Decimal("1.5"), **pair[1])
    assert e.value.retry_at == (frozen + timedelta(days=1)).replace(hour=0)
    for r in held:
        store.release(r)
    assert budget.pace_limit(Decimal("20"), frozen.replace(day=days)) == Decimal("20")  # whole budget by month end


def test_a_paced_task_waits_for_tomorrow_without_failing_or_penalising_the_agent(scripted, tenant, monkeypatch):
    scripted()
    tid, _ = tenant
    tomorrow = datetime.now(timezone.utc) + timedelta(hours=10)
    monkeypatch.setattr(budget_guard, "pace_limit", lambda b, now=None: Decimal("0.0001"))
    monkeypatch.setattr(budget_guard, "next_day_start", lambda now=None: tomorrow)
    with tenant_session(tid) as s:
        ceo = _active(s)["ceo"]
        task, _ = workflows.enqueue(s, kind="agent_run", agent_id=ceo.id, idempotency_key="p1",
                                    payload={"instruction": "Plan the week"})
        task_id = task.id
    workflows.drain()
    with tenant_session(tid) as s:
        t = s.get(Task, task_id)
        assert t.status == "queued" and t.attempts == 0 and "spending pace" in t.last_error
        assert t.run_after.replace(tzinfo=timezone.utc) == tomorrow
        assert s.get(Agent, ceo.id).consecutive_failures == 0 and s.get(Agent, ceo.id).status == "active"
        assert s.execute(select(func.count(CostEntry.id)).where(CostEntry.task_id == task_id)).scalar_one() == 0
        assert s.scalars(select(AuditLog).where(AuditLog.action == "task.deferred")).first() is not None


def test_a_simulated_month_never_overspends_and_can_use_the_whole_budget(monkeypatch):
    monkeypatch.setenv("WWOS_BUDGET_PACING_BURST_PCT", "25")
    monkeypatch.setenv("WWOS_HARD_MONTHLY_CAP_USD", "20")
    tid, _ = _zero_tenant(monkeypatch)
    with tenant_session(tid) as s:
        ai_plan.apply(s, s.get(Tenant, tid), Decimal("20"))
        roster = list(_active(s).values())
    now = datetime.now(timezone.utc)
    days = __import__("calendar").monthrange(now.year, now.month)[1]
    store = budget_guard.store()
    total = Decimal("0")
    for day in range(1, days + 1):
        frozen = _freeze(monkeypatch, day)
        for _ in range(60):  # far more demand than the budget allows
            a = roster[_ % len(roster)]
            try:
                rid = store.reserve(tenant_id=tid, agent_id=a.id, agent_name=a.name,
                                    agent_budget=a.monthly_budget_usd, task_id=None, estimate=Decimal("0.12"))
            except BudgetExceeded:
                continue
            total += store.incur(rid, model="claude-sonnet-5-5", input_tokens=4000, output_tokens=8000)
            store.spill([rid])
        assert total <= budget.pace_limit(Decimal("20"), frozen) <= Decimal("20")
    with tenant_session(tid) as s:
        ledger = s.execute(select(func.sum(CostEntry.cost_usd))).scalar_one()
    assert Decimal(str(ledger)) == total <= Decimal("20")
    assert total >= Decimal("14")  # agent shares (74%) are usable; nothing is left stranded by pacing


def test_weekly_cadence_runs_ai_reviews_once_a_week(monkeypatch, tenant):
    monkeypatch.setenv("WWOS_AI_REVIEW_CADENCE", "weekly")
    tid, _ = tenant
    monday = datetime(2026, 11, 23, 9, tzinfo=timezone.utc)
    for d in range(7):
        schedule.tick(monday + timedelta(days=d))
    schedule.tick(monday + timedelta(days=7))
    with tenant_session(tid) as s:
        keys = [k for (k,) in s.execute(select(Task.idempotency_key))]
    assert sorted(k for k in keys if k.startswith("sched:ceo")) == ["sched:ceo-daily-review:2026-W48",
                                                                    "sched:ceo-daily-review:2026-W49"]
    assert len([k for k in keys if k.startswith("sched:daily-briefing")]) == 8  # the free briefing stays daily


def test_budget_view_shows_what_is_usable_today(monkeypatch, tenant):
    monkeypatch.setenv("WWOS_BUDGET_PACING_BURST_PCT", "25")
    tid, key = tenant
    body = TestClient(app).get("/api/budget", headers={"Authorization": f"Bearer {key}"}).json()
    assert body["pacing"] is True and Decimal(body["available_now_usd"]) > 0
    assert Decimal(body["usable_so_far_usd"]) <= Decimal(body["monthly_budget_usd"])


def _env_file(name):
    pairs = [line.split("=", 1) for line in (ROOT / "deploy" / name).read_text().splitlines()
             if line and not line.startswith("#") and "=" in line]
    return {k: v for k, v in pairs}


@pytest.mark.parametrize("name", ["env.zero-cost.example", "env.low-cost.example"])
def test_shipped_profiles_pass_production_checks(monkeypatch, name):
    from wwos.config import production_problems

    for k, v in {**_env_file(name), "WWOS_DATABASE_URL": "postgresql+psycopg://u:p@db/w"}.items():
        monkeypatch.setenv(k, v)
    assert production_problems() == []


def test_low_cost_profile_caps_spend_at_20_dollars():
    env = _env_file("env.low-cost.example")
    assert env["WWOS_HARD_MONTHLY_CAP_USD"] == "20" and env["WWOS_ORG_MONTHLY_BUDGET_USD"] == "20"
    assert env["WWOS_LLM_PROVIDER"] == "anthropic" and env["WWOS_AGENT_RUNTIME"] == "messages"


def test_a_paced_department_delays_an_objective_instead_of_failing_it(tenant, monkeypatch):
    """Mid-objective, Finance is out of today's allowance: its step waits, nothing is marked failed or
    reassigned, and the objective completes once the allowance returns."""
    from tests.simulated_org import SimulatedOrganisation
    from wwos.models import Objective, ObjectiveTask

    tid, key = tenant
    H = {"Authorization": f"Bearer {key}"}
    paced = {"on": True}
    real_admit = budget_guard._admit

    def admit(**kw):
        if paced["on"] and kw["agent_name"] == "Finance Controller":
            raise PaceDeferred("spending pace: test", retry_at=datetime.now(timezone.utc) + timedelta(hours=8))
        return real_admit(**kw)
    monkeypatch.setattr(budget_guard, "_admit", admit)
    with TestClient(app) as c:
        c.post("/api/leads", headers=H, json={
            "email": "maria@northwind.io", "first_name": "Maria", "company": "Northwind Logistics",
            "need": "We need a cost review across our three depots and suppliers", "estimated_value": "42000"})
        c.post("/api/worker/drain", headers=H)
        deal_id = c.get("/api/deals", headers=H).json()[0]["id"]
        partner = c.post("/api/partners", headers=H, json={
            "name": "Pune Analytics Pvt Ltd", "region": "india", "contact_email": "projects@pune-analytics.example",
            "services": "data analysis"}).json()
        c.post(f"/api/partners/{partner['id']}/nda", json={"on_file": True}, headers=H)
        c.post(f"/api/partners/{partner['id']}/confirm", json={"agreement_on_file": True}, headers=H)
        llm.set_provider(SimulatedOrganisation())
        obj = c.post("/api/objectives", headers=H, json={
            "title": "Win the Northwind depot review", "deal_id": deal_id,
            "description": "Move the Northwind opportunity forward, involve our Indian analytics partner for the "
                           "data work, confirm budget headroom and prepare marketing collateral."}).json()
        workflows.drain(max_tasks=200)
        with tenant_session(tid) as s:
            fin = s.scalars(select(ObjectiveTask).where(ObjectiveTask.department_code == "finance")).one()
            assert fin.status not in ("failed", "passed") and fin.attempts <= 1
            waiting = s.scalars(select(Task).where(Task.status == "queued", Task.last_error.like("deferred:%"))).all()
            assert waiting and all(t.run_after.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
                                   for t in waiting)
            assert s.get(Objective, obj["id"]).status not in ("completed", "failed")
            fin_agent = s.scalars(select(Agent).where(Agent.role == "finance", Agent.status == "active")).one()
            assert fin_agent.consecutive_failures == 0
            for t in waiting:  # the next day arrives
                t.run_after = datetime.now(timezone.utc) - timedelta(seconds=1)
        paced["on"] = False
        workflows.drain(max_tasks=200)
        result = c.get(f"/api/objectives/{obj['id']}", headers=H).json()
    assert result["status"] == "completed", result
