"""Recurring autonomous operations.

The worker calls ``tick()`` on every poll. Each job's idempotency key embeds
its period (day or hour), so every job runs exactly once per period per tenant
no matter how many workers are running or how often they poll.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from .agents.runtime import find_agent
from .db import system_session, tenant_session
from .models import Tenant, utcnow

log = logging.getLogger("wwos.schedule")


@dataclass(frozen=True)
class Job:
    name: str
    role: str
    period: str  # "daily" | "hourly"
    instruction: str


JOBS = [
    Job("ceo-daily-review", "ceo", "daily",
        "Daily operating review. Check the pipeline (crm.pipeline_summary), budget (budget.status) and the "
        "agent workforce (agents.list). Delegate concrete follow-ups to department agents with tasks.delegate. "
        "If a recurring need is not covered by any agent, provision a specialist within budget. "
        "Finish with a five-line briefing for the owner."),
    Job("ops-daily-health", "operations", "daily",
        "Daily operations check. Review pipeline movement and budget status, identify stalled work, and "
        "delegate follow-ups to the responsible agent. Report anything that needs the owner's attention."),
]

_last_qa: dict = {"at": None, "passed": None, "summary": ""}


def _period_key(period: str, now: datetime) -> str:
    return now.strftime("%Y-%m-%d") if period == "daily" else now.strftime("%Y-%m-%dT%H")


def tick(now: datetime | None = None) -> int:
    """Enqueue any due recurring jobs. Returns how many tasks were newly created."""
    from .workflows import enqueue

    now = now or utcnow()
    created = 0
    with system_session() as s:
        tenant_ids = [t for (t,) in s.execute(select(Tenant.id)).all()]
    for tid in tenant_ids:
        with tenant_session(tid) as s:
            for job in JOBS:
                agent = find_agent(s, role=job.role)
                if agent is None:
                    continue
                _, new = enqueue(s, kind="agent_run", agent_id=agent.id,
                                 idempotency_key=f"sched:{job.name}:{_period_key(job.period, now)}",
                                 payload={"instruction": job.instruction, "delegated_by": f"schedule:{job.name}"})
                created += int(new)
    hour = _period_key("hourly", now)
    if _last_qa["at"] != hour:
        from . import qa

        report = qa.run()
        _last_qa.update(at=hour, passed=report.passed,
                        summary=f"{sum(c.passed for c in report.checks)}/{len(report.checks)} checks passed")
        (log.info if report.passed else log.error)("QA scan: %s", _last_qa["summary"])
    return created


def last_qa() -> dict:
    return dict(_last_qa)
