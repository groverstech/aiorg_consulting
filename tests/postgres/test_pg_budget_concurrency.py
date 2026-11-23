"""Concurrent budget enforcement on PostgreSQL (the production database)."""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from wwos import budget_guard, workflows
from wwos.agents.runtime import find_agent
from wwos.budget import BudgetExceeded
from wwos.db import system_session, tenant_session
from wwos.models import BudgetReservation, CostEntry, Task, utcnow

pytestmark = pytest.mark.postgres


def _ledger(tid):
    with system_session() as s:
        return Decimal(str(s.execute(select(func.coalesce(func.sum(CostEntry.cost_usd), 0))
                                     .where(CostEntry.tenant_id == tid)).scalar_one()))


def test_store_is_postgres(pg, tenant):
    assert isinstance(budget_guard.store(), budget_guard.SqlReservationStore)


def test_concurrent_reservations_never_exceed_org_budget(pg):
    from wwos.bootstrap import create_tenant

    tid, _ = create_tenant("Tight Co", "10", with_agents=False)
    guard = budget_guard.store()
    barrier = threading.Barrier(24)
    results = []

    def attempt(i):
        barrier.wait()
        try:
            rid = guard.reserve(tenant_id=tid, agent_id=f"agent-{i % 3}", agent_name="x", agent_budget=Decimal("100"),
                                task_id=None, estimate=Decimal("1"))
            guard.incur(rid, model="claude-opus-5-5", input_tokens=0, output_tokens=50_000)  # $1.00
            guard.spill([rid])
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(24) as ex:
        results = list(ex.map(attempt, range(24)))
    assert sum(results) == 10
    assert _ledger(tid) == Decimal("10")


def test_concurrent_reservations_respect_per_agent_budget(pg):
    from wwos.bootstrap import create_tenant

    tid, _ = create_tenant("Agent Cap Co", "1000", with_agents=False)
    guard = budget_guard.store()
    barrier = threading.Barrier(16)

    def attempt(_):
        barrier.wait()
        try:
            guard.reserve(tenant_id=tid, agent_id="one-agent", agent_name="x", agent_budget=Decimal("3"),
                          task_id=None, estimate=Decimal("1"))
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(16) as ex:
        assert sum(ex.map(attempt, range(16))) == 3


def test_concurrent_workers_full_runs_stay_within_budget(pg, scripted, monkeypatch):
    """Four worker threads process 30 agent tasks against a budget that admits only some calls."""
    from wwos.bootstrap import create_tenant

    monkeypatch.setenv("WWOS_AGENT_FAILURE_SUSPEND_THRESHOLD", "999")
    tid, _ = create_tenant("Busy Co", "500")
    scripted(input_tokens=100, output_tokens=1000)  # $0.0204 per call; estimate ~ $0.08 at max_tokens 4096
    with tenant_session(tid) as s:
        ops = find_agent(s, role="operations")
        ops.monthly_budget_usd = Decimal("0.50")
        for i in range(30):
            workflows.enqueue(s, kind="agent_run", idempotency_key=f"k{i}", agent_id=ops.id,
                              payload={"instruction": f"job {i}"})
        ops_id = ops.id

    def worker():
        workflows.run_worker(stop_when_idle=True, tenant_id=tid)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with tenant_session(tid) as s:
        statuses = [t.status for t in s.scalars(select(Task))]
        spent = Decimal(str(s.execute(select(func.coalesce(func.sum(CostEntry.cost_usd), 0))
                                      .where(CostEntry.agent_id == ops_id)).scalar_one()))
    print("succeeded", statuses.count("succeeded"), "dead", statuses.count("dead"), "spent", spent)
    assert statuses.count("succeeded") > 0 and statuses.count("dead") > 0  # both paths exercised
    assert spent <= Decimal("0.50")
    assert statuses.count("succeeded") == int(spent / Decimal("0.0204"))
    assert statuses.count("succeeded") + statuses.count("dead") == 30
    assert budget_guard.store().active_count() == 0


def test_crashed_worker_reservations_reconciled_exactly_once(pg, tenant):
    tid, _ = tenant
    guard = budget_guard.store()
    rid = guard.reserve(tenant_id=tid, agent_id="a", agent_name="a", agent_budget=Decimal("50"), task_id=None,
                        estimate=Decimal("1"))
    guard.incur(rid, model="claude-opus-5-5", input_tokens=1000, output_tokens=1000)
    lost = guard.reserve(tenant_id=tid, agent_id="a", agent_name="a", agent_budget=Decimal("50"), task_id=None,
                         estimate=Decimal("1"))  # crashed before the call returned
    with system_session() as s:
        s.execute(update(BudgetReservation).values(expires_at=utcnow() - timedelta(seconds=1)))
    assert guard.reconcile_expired() == 2
    assert guard.reconcile_expired() == 0
    assert _ledger(tid) == Decimal("0.024")
    with system_session() as s:
        st = dict(s.execute(select(BudgetReservation.id, BudgetReservation.status)).all())
    assert st[rid] == "settled" and st[lost] == "released"


def test_rollback_after_spend_still_records_once(pg, tenant, scripted):
    from wwos.agents.runtime import run_agent_prompt
    from wwos.models import Tenant

    tid, _ = tenant
    scripted(input_tokens=1000, output_tokens=1000)
    with pytest.raises(RuntimeError):
        with tenant_session(tid) as s:
            run_agent_prompt(s, s.get(Tenant, tid), find_agent(s, role="qa"), "hello")
            raise RuntimeError("business failure after the model call")
    assert _ledger(tid) == Decimal("0.024")
    assert budget_guard.store().active_count() == 0


def test_installation_hard_cap_holds_across_tenants_under_concurrency(pg, monkeypatch):
    """24 threads across two tenants, each tenant with $10 of budget, under a $7 installation cap."""
    from wwos.bootstrap import create_tenant

    monkeypatch.setenv("WWOS_HARD_MONTHLY_CAP_USD", "7")
    tenants = [create_tenant(n, "10", with_agents=False)[0] for n in ("Cap A", "Cap B")]
    guard = budget_guard.store()
    barrier = threading.Barrier(24)

    def attempt(i):
        barrier.wait()
        try:
            rid = guard.reserve(tenant_id=tenants[i % 2], agent_id=f"agent-{i % 4}", agent_name="x",
                                agent_budget=Decimal("100"), task_id=None, estimate=Decimal("1"))
            guard.incur(rid, model="claude-opus-5-5", input_tokens=0, output_tokens=50_000)  # $1.00
            guard.spill([rid])
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(24) as ex:
        results = list(ex.map(attempt, range(24)))
    assert sum(results) == 7
    assert _ledger(tenants[0]) + _ledger(tenants[1]) == Decimal("7")


def test_pacing_defers_on_postgres(pg, monkeypatch):
    from wwos.budget import PaceDeferred
    from wwos.bootstrap import create_tenant

    monkeypatch.setenv("WWOS_BUDGET_PACING_BURST_PCT", "0")
    tid, _ = create_tenant("Paced Co", "30", with_agents=False)
    monkeypatch.setattr(budget_guard, "pace_limit", lambda b, now=None: Decimal("2"))
    guard = budget_guard.store()
    held = [guard.reserve(tenant_id=tid, agent_id="a", agent_name="x", agent_budget=Decimal("100"), task_id=None,
                          estimate=Decimal("1")) for _ in range(2)]
    with pytest.raises(PaceDeferred):
        guard.reserve(tenant_id=tid, agent_id="a", agent_name="x", agent_budget=Decimal("100"), task_id=None,
                      estimate=Decimal("1"))
    for r in held:
        guard.release(r)
