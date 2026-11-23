"""Zero-spend mode (WWOS_LLM_PROVIDER=none, $0 budgets, manual e-mail): the business keeps running on
the rules engine and nothing can ever call a paid model."""
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from wwos import comms, llm, schedule, workflows
from wwos.api import app
from wwos.db import system_session, tenant_session
from wwos.models import (Activity, Agent, ApprovalRequest, CostEntry, Deal, Document, OutboundMessage, Task, utcnow)

pytestmark = pytest.mark.acceptance

LEAD = {"email": "jane@acme.com", "first_name": "Jane", "company": "Acme Ltd",
        "need": "We need help redesigning our operations model for growth", "estimated_value": "25000"}


@pytest.fixture
def zero(monkeypatch):
    monkeypatch.setenv("WWOS_LLM_PROVIDER", "none")
    monkeypatch.setenv("WWOS_EMAIL_TRANSPORT", "manual")
    monkeypatch.setenv("WWOS_ORG_MONTHLY_BUDGET_USD", "0")

    def boom(*a, **k):
        raise AssertionError("a model provider was called in zero-spend mode")
    monkeypatch.setattr(llm.AnthropicProvider, "complete", boom)
    monkeypatch.setattr(llm.OfflineProvider, "complete", boom)
    from wwos.bootstrap import create_tenant

    return create_tenant("White Waters Consulting Group")  # default budget: $0


def H(k):
    return {"Authorization": f"Bearer {k}"}


def test_tenant_starts_on_zero_budget_with_working_departments(zero):
    tid, _ = zero
    with tenant_session(tid) as s:
        agents = s.scalars(select(Agent)).all()
        assert len(agents) == 9 and all(a.status == "active" for a in agents)
        assert all(a.monthly_budget_usd == 0 for a in agents)
        assert all("AI off" in a.status_reason for a in agents)


def test_full_business_cycle_runs_without_ai_or_mail_account(zero):
    tid, key = zero
    with TestClient(app) as c:
        assert c.post("/api/leads", json=LEAD, headers=H(key)).status_code == 202
        c.post("/api/worker/drain", headers=H(key))
        deal = c.get("/api/deals", headers=H(key)).json()[0]
        assert deal["stage"] == "proposal"
        [approval] = c.get("/api/approvals", headers=H(key)).json()
        assert "Dear Jane" in approval["preview"]["body"]
        c.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True}, headers=H(key))
        c.post("/api/worker/drain", headers=H(key))
        msg = c.get("/api/messages", headers=H(key)).json()[0]
        assert msg["status"] == "manual"  # waits for the owner to send it by hand; never claimed as sent
        assert comms.OutboxTransport.delivered == []
        c.post("/login", data={"api_key": key})
        page = c.get("/dashboard").text
        assert "E-mails for you to send" in page and "off (zero-spend mode)" in page
        r = c.post(f"/dashboard/messages/{msg['id']}/sent", follow_redirects=False)
        assert r.status_code == 303
        assert c.get("/api/messages", headers=H(key)).json()[0]["status"] == "sent"
        for stage in ["negotiation", "won"]:
            c.post(f"/api/deals/{deal['id']}/stage", json={"stage": stage}, headers=H(key))
        c.post("/api/worker/drain", headers=H(key))
    with tenant_session(tid) as s:
        assert s.scalars(select(Document).where(Document.kind == "sow")).one()
        assert s.scalars(select(ApprovalRequest).where(ApprovalRequest.action == "legal.sign_agreement")).one()
        notes = [a.summary for a in s.scalars(select(Activity))]
        assert any("Next step for the owner" in n for n in notes)
        assert s.execute(select(func.count(CostEntry.id))).scalar_one() == 0
        assert not s.scalars(select(Task).where(Task.status.in_(["dead"]))).all()


def test_ai_only_features_refuse_clearly(zero):
    tid, key = zero
    with TestClient(app) as c:
        sales = next(a for a in c.get("/api/agents", headers=H(key)).json() if a["role"] == "sales")
        r = c.post("/api/tasks", json={"agent_id": sales["id"], "instruction": "do things"}, headers=H(key))
        assert r.status_code == 409 and "zero-spend" in r.json()["detail"]
        r = c.post("/api/objectives", json={"title": "Grow", "description": "x" * 40}, headers=H(key))
        assert r.status_code == 400 and "zero-spend" in r.json()["detail"]
    with tenant_session(tid) as s:  # internally queued AI work is marked skipped, never faked or failed
        a = s.scalars(select(Agent).where(Agent.role == "operations")).one()
        workflows.enqueue(s, kind="agent_run", idempotency_key="internal", agent_id=a.id, payload={"instruction": "x"})
    workflows.drain()
    with tenant_session(tid) as s:
        t = s.scalars(select(Task).where(Task.idempotency_key == "internal")).one()
        assert t.status == "skipped" and ("AI is off" in t.last_error or "needs AI" in t.last_error)
        assert s.scalars(select(Agent).where(Agent.role == "operations")).one().consecutive_failures == 0


def test_daily_briefing_and_stalled_deal_follow_ups(zero):
    from datetime import datetime, timezone

    tid, key = zero
    with TestClient(app) as c:
        c.post("/api/leads", json=LEAD, headers=H(key))
        c.post("/api/worker/drain", headers=H(key))
    with system_session() as s:  # the proposal has had no activity for 8 days
        s.execute(update(Activity).values(created_at=utcnow() - timedelta(days=8)))
    day = datetime.now(timezone.utc)
    assert schedule.tick(day) == 1  # only the rules briefing is scheduled when AI is off
    workflows.drain()
    with tenant_session(tid) as s:
        doc = s.scalars(select(Document).where(Document.kind == "briefing")).one()
        assert "Engagement - Acme Ltd" in doc.markdown and "Follow-up e-mails drafted for your approval: 1" in doc.markdown
        followup = s.scalars(select(OutboundMessage).where(OutboundMessage.subject.like("Following up%"))).one()
        assert followup.status == "pending_approval"
        assert s.scalars(select(ApprovalRequest).where(ApprovalRequest.status == "pending")).all()
    # A second briefing in the same week (e.g. a re-run) never drafts a duplicate follow-up.
    with system_session() as s:
        s.execute(update(Activity).values(created_at=utcnow() - timedelta(days=8)))
    with tenant_session(tid) as s:
        workflows.enqueue(s, kind="workflow.daily_briefing", idempotency_key="rerun", payload={})
    workflows.drain()
    with tenant_session(tid) as s:
        assert len(s.scalars(select(OutboundMessage).where(OutboundMessage.subject.like("Following up%"))).all()) == 1


def test_even_a_misconfigured_live_provider_cannot_spend_on_zero_budget(zero, monkeypatch):
    from wwos.agents.runtime import find_agent, run_agent_prompt
    from wwos.budget import BudgetExceeded
    from wwos.models import Tenant

    tid, _ = zero
    monkeypatch.setenv("WWOS_LLM_PROVIDER", "anthropic")  # someone switches AI on but leaves budgets at $0
    with tenant_session(tid) as s:
        with pytest.raises(BudgetExceeded):
            run_agent_prompt(s, s.get(Tenant, tid), find_agent(s, role="sales"), "hello")  # provider never reached


def test_email_daily_limit(tenant, monkeypatch):
    tid, _ = tenant
    monkeypatch.setenv("WWOS_EMAIL_DAILY_LIMIT", "2")
    with tenant_session(tid) as s:
        for i in range(5):
            m, _ = comms.draft_email(s, idempotency_key=f"c{i}", to=f"a{i}@b.com", subject="s", body="b")
            comms.request_send(s, m.id)
    comms.set_transport(comms.OutboxTransport())
    comms.dispatch()
    comms.dispatch()
    assert len(comms.OutboxTransport.delivered) == 2
    with tenant_session(tid) as s:
        assert s.execute(select(func.count(OutboundMessage.id)).where(OutboundMessage.status == "queued")).scalar_one() == 3


@pytest.mark.parametrize("env,ok", [
    ({"WWOS_LLM_PROVIDER": "none", "WWOS_EMAIL_TRANSPORT": "manual", "WWOS_PUBLIC_ORIGIN": "http://localhost:8000",
      "WWOS_COOKIE_SECURE": "false"}, True),
    ({"WWOS_LLM_PROVIDER": "offline"}, False),
    ({"WWOS_EMAIL_TRANSPORT": "outbox"}, False),
    ({"WWOS_PUBLIC_ORIGIN": "http://ops.example.com", "WWOS_COOKIE_SECURE": "false"}, False),
])
def test_zero_cost_production_profile(monkeypatch, env, ok):
    from wwos.config import production_problems

    base = {"WWOS_ENV": "production", "WWOS_DATABASE_URL": "postgresql+psycopg://u:p@db/w", "WWOS_LLM_PROVIDER": "none",
            "WWOS_EMAIL_TRANSPORT": "manual", "WWOS_PUBLIC_ORIGIN": "http://localhost:8000", "WWOS_COOKIE_SECURE": "false"}
    for k, v in {**base, **env}.items():
        monkeypatch.setenv(k, v)
    assert (production_problems() == []) is ok, production_problems()
