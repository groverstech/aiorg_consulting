import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from wwos import budget_guard
from wwos.budget import BudgetExceeded
from wwos.db import system_session
from wwos.models import CostEntry


def _ledger(tid):
    with system_session() as s:
        return Decimal(str(s.execute(select(func.coalesce(func.sum(CostEntry.cost_usd), 0))
                                     .where(CostEntry.tenant_id == tid)).scalar_one()))


def test_memory_store_thread_race_respects_budget():
    from wwos.bootstrap import create_tenant

    tid, _ = create_tenant("Tight Co", "10", with_agents=False)
    guard = budget_guard.store()
    assert isinstance(guard, budget_guard.MemoryReservationStore)
    barrier = threading.Barrier(24)

    def attempt(i):
        barrier.wait()
        try:
            rid = guard.reserve(tenant_id=tid, agent_id="a", agent_name="a", agent_budget=Decimal("100"),
                                task_id=None, estimate=Decimal("1"))
            guard.incur(rid, model="claude-opus-5-5", input_tokens=0, output_tokens=50_000)
            guard.spill([rid])
            return True
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(24) as ex:
        assert sum(ex.map(attempt, range(24))) == 10
    assert _ledger(tid) == Decimal("10")


def test_released_reservation_frees_budget_and_spill_is_idempotent(tenant):
    tid, _ = tenant
    guard = budget_guard.store()
    kw = dict(tenant_id=tid, agent_id="a", agent_name="a", agent_budget=Decimal("2"), task_id=None)
    r1 = guard.reserve(estimate=Decimal("1.5"), **kw)
    with pytest.raises(BudgetExceeded):
        guard.reserve(estimate=Decimal("1"), **kw)
    guard.release(r1)  # provider failed before billing
    guard.settle([r1])
    r2 = guard.reserve(estimate=Decimal("1"), **kw)
    guard.incur(r2, model="claude-haiku-5-5", input_tokens=1_000_000, output_tokens=0)  # $0.10
    guard.spill([r2])
    guard.spill([r2])
    assert _ledger(tid) == Decimal("0.1")
