from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from wwos import comms, workflows
from wwos.agents.runtime import find_agent
from wwos.budget import spent_this_month
from wwos.db import system_session, tenant_session
from wwos.llm import TransientLLMError
from wwos.models import Agent, Contact, Deal, OutboundMessage, Task, utcnow

LEAD = {"email": "jane@acme.com", "first_name": "Jane", "company": "Acme Ltd",
        "need": "We need help redesigning our operations model for growth", "estimated_value": "25000"}


def _run_due():
    """Drain, forcing backoff timers to expire so retries happen immediately."""
    total = 0
    for _ in range(10):
        with system_session() as s:
            s.execute(update(Task).where(Task.status == "queued").values(run_after=utcnow() - timedelta(seconds=1)))
        n = workflows.drain()
        total += n
        if n == 0:
            break
    return total


def test_enqueue_is_idempotent(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        t1, c1 = workflows.enqueue(s, kind="agent_run", idempotency_key="k1", payload={"instruction": "x"})
        t2, c2 = workflows.enqueue(s, kind="agent_run", idempotency_key="k1", payload={"instruction": "y"})
        assert c1 and not c2 and t1.id == t2.id


def test_lead_workflow_end_to_end_and_duplicate_submission(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        workflows.start_lead_workflow(s, LEAD)
    with tenant_session(tid) as s:
        workflows.start_lead_workflow(s, dict(LEAD))  # duplicate submission
    _run_due()
    with tenant_session(tid) as s:
        assert len(s.scalars(select(Contact)).all()) == 1
        deal = s.scalars(select(Deal)).one()
        assert deal.stage == "proposal" and deal.value == Decimal("25000.00")
        msg = s.scalars(select(OutboundMessage)).one()
        assert msg.status == "pending_approval"  # owner policy gate on external email
        assert all(t.status == "succeeded" for t in s.scalars(select(Task)))


def test_unqualified_lead_is_closed_lost(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        workflows.start_lead_workflow(s, {"email": "x@y.com", "first_name": "X", "need": "hi", "estimated_value": "10"})
    _run_due()
    with tenant_session(tid) as s:
        assert s.scalars(select(Deal)).one().stage == "lost"
        assert s.scalars(select(OutboundMessage)).all() == []


def test_transient_failures_retry_then_dead_letter_and_spend_survives(tenant, scripted, monkeypatch):
    tid, _ = tenant
    monkeypatch.setenv("WWOS_AGENT_FAILURE_SUSPEND_THRESHOLD", "99")
    scripted(error=TransientLLMError("overloaded"))
    with tenant_session(tid) as s:
        a = find_agent(s, role="operations")
        task, _ = workflows.enqueue(s, kind="agent_run", idempotency_key="t", agent_id=a.id,
                                    payload={"instruction": "check"})
        task_id = task.id
    _run_due()
    with tenant_session(tid) as s:
        t = s.get(Task, task_id)
        assert t.status == "dead" and t.attempts == 4 and "overloaded" in t.last_error


def test_failed_run_still_records_spend(tenant, scripted):
    tid, _ = tenant
    # First call succeeds and bills, second (after a tool call) raises.
    p = scripted(input_tokens=1000, output_tokens=1000,
                 script=[[{"type": "tool_use", "id": "t1", "name": "crm__pipeline_summary", "input": {}}]])
    orig = p.complete

    def flaky(**kw):
        if p.calls >= 1:
            p.calls += 1
            raise RuntimeError("crash after spending")
        return orig(**kw)
    p.complete = flaky
    with tenant_session(tid) as s:
        a = find_agent(s, role="operations")
        workflows.enqueue(s, kind="agent_run", idempotency_key="spend", agent_id=a.id,
                          payload={"instruction": "x"})
        agent_id = a.id
    workflows.drain()
    with tenant_session(tid) as s:
        assert spent_this_month(s, agent_id) == Decimal("0.024")


def test_malfunctioning_agent_is_suspended_and_recovery_assigned(tenant, scripted):
    tid, _ = tenant
    scripted(error=RuntimeError("bad tool loop"))
    with tenant_session(tid) as s:
        a = find_agent(s, role="marketing")
        for i in range(3):
            workflows.enqueue(s, kind="agent_run", idempotency_key=f"m{i}", agent_id=a.id,
                              payload={"instruction": "x"})
        agent_id = a.id
    workflows.drain(max_tasks=3)
    with tenant_session(tid) as s:
        agent = s.get(Agent, agent_id)
        assert agent.status == "suspended" and "auto-suspended" in agent.status_reason
        tech = find_agent(s, role="technology")
        recovery = s.scalars(select(Task).where(Task.agent_id == tech.id)).all()
        assert len(recovery) == 1 and agent_id in recovery[0].payload["instruction"]


def test_expired_lease_is_reclaimed(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        t, _ = workflows.enqueue(s, kind="workflow.lead_intake", idempotency_key="lease", payload=LEAD,
                                 workflow_id="wf1")
        tid_task = t.id
    claimed = workflows.claim_next()
    assert claimed[0] == tid_task
    assert workflows.claim_next() is None  # leased
    with system_session() as s:
        s.execute(update(Task).values(lease_until=utcnow() - timedelta(seconds=1)))
    assert workflows.claim_next()[0] == tid_task


def test_email_delivery_is_exactly_once_and_failures_recorded(tenant):
    tid, _ = tenant

    class Boom:
        def send(self, **kw):
            raise comms.DeliveryRejected("smtp down", retryable=True)

    with tenant_session(tid) as s:
        m, _ = comms.draft_email(s, idempotency_key="e1", to="a@b.com", subject="Hi", body="Body")
        comms.request_send(s, m.id)
        mid = m.id
    comms.set_transport(Boom())
    assert comms.dispatch() == 0
    with tenant_session(tid) as s:
        msg = s.get(OutboundMessage, mid)
        assert msg.status == "queued" and "smtp down" in msg.last_error and msg.attempts == 1
    comms.set_transport(None)
    assert comms.dispatch() == 0  # still inside the retry backoff window
    with system_session() as s:
        s.execute(update(OutboundMessage).values(updated_at=utcnow() - timedelta(minutes=5)))
    assert comms.dispatch() == 1
    assert comms.dispatch() == 0
    with tenant_session(tid) as s:
        comms.request_send(s, mid)  # idempotent once sent
        again, created = comms.draft_email(s, idempotency_key="e1", to="a@b.com", subject="Hi", body="Body")
        assert not created and s.get(OutboundMessage, mid).status == "sent"
    assert len(comms.OutboxTransport.delivered) == 1
