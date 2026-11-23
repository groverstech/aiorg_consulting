from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from wwos import schedule, workflows
from wwos.db import tenant_session
from wwos.models import Task


def test_recurring_jobs_run_exactly_once_per_period(tenant):
    tid, _ = tenant
    day = datetime(2026, 11, 22, 9, tzinfo=timezone.utc)
    assert schedule.tick(day) == 3  # rules briefing + CEO review + operations check
    assert schedule.tick(day + timedelta(hours=3)) == 0  # same day: nothing new
    assert schedule.tick(day + timedelta(days=1)) == 3
    workflows.drain()
    with tenant_session(tid) as s:
        tasks = s.scalars(select(Task).where(Task.idempotency_key.like("sched:%"))).all()
        assert len(tasks) == 6 and all(t.status == "succeeded" for t in tasks)
    assert schedule.last_qa()["passed"] is True
