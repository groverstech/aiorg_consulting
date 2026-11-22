"""Autonomous organisation validation (mandate section 6), with a simulated model.

After the owner states an objective, no human acts until the executive report exists: the
worker (``drain``) is the only driver. The simulated model makes the decisions a model would;
the runtime, gates, budgets and coordinator are the real system.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.simulated_org import SimulatedOrganisation
from wwos import llm, qa, workflows
from wwos.api import app
from wwos.db import tenant_session
from wwos.models import Activity, Agent, ApprovalRequest, Deal, Document

pytestmark = pytest.mark.acceptance

LEAD = {"email": "maria@northwind.io", "first_name": "Maria", "last_name": "Lopez",
        "company": "Northwind Logistics", "need": "We need a cost review across our three depots and suppliers",
        "estimated_value": "42000"}


def H(k):
    return {"Authorization": f"Bearer {k}"}


def test_ceo_runs_an_objective_end_to_end_without_human_intervention(tenant):
    run_ceo_scenario(tenant, llm.set_provider)


def run_ceo_scenario(tenant, install_model):
    """``install_model(sim)`` makes the simulated model drive the agents (provider for the Messages
    runtime; a fake HTTP API for the Agent SDK runtime)."""
    tid, key = tenant
    sim = SimulatedOrganisation(failing_agents={"Marketing Manager"}, qa_reject_once={"finance"})
    with TestClient(app) as c:
        # Owner setup (before the objective): a lead in the pipeline and a partner with an NDA on file.
        c.post("/api/leads", json=LEAD, headers=H(key))
        c.post("/api/worker/drain", headers=H(key))
        deal_id = c.get("/api/deals", headers=H(key)).json()[0]["id"]
        partner = c.post("/api/partners", headers=H(key), json={
            "name": "Pune Analytics Pvt Ltd", "region": "india", "contact_email": "projects@pune-analytics.example",
            "services": "data analysis"}).json()
        c.post(f"/api/partners/{partner['id']}/nda", json={"on_file": True}, headers=H(key))
        c.post(f"/api/partners/{partner['id']}/confirm", json={"agreement_on_file": True}, headers=H(key))

        # 1. The owner gives the CEO a business objective.
        install_model(sim)
        obj = c.post("/api/objectives", headers=H(key), json={
            "title": "Win the Northwind depot review",
            "description": "Move the Northwind opportunity forward, involve our Indian analytics partner for the "
                           "data work, confirm budget headroom and prepare marketing collateral.",
            "deal_id": deal_id}).json()

        # 2-9. Only the worker runs from here.
        processed = workflows.drain(max_tasks=200)
        assert processed > 10

        result = c.get(f"/api/objectives/{obj['id']}", headers=H(key)).json()
    assert result["status"] == "completed", result
    tasks = {t["title"]: t for t in result["tasks"]}
    assert len(tasks) == 4 and all(t["status"] == "passed" for t in tasks.values())

    with tenant_session(tid) as s:
        # 3-4. Missing capability identified; specialist created by the CEO, tested, inheriting a subset of
        # the CEO's permissions and a small budget.
        coord = s.scalars(select(Agent).where(Agent.name == "Partner Coordinator")).one()
        ceo = s.scalars(select(Agent).where(Agent.role == "ceo")).one()
        assert coord.created_by == "CEO" and coord.status == "active" and coord.status_reason == "acceptance test passed"
        assert set(coord.tools) <= set(ceo.tools) and not coord.can_provision_agents
        assert coord.monthly_budget_usd <= 5

        # 5. Work assigned within permissions: the specialist did the partner task.
        assert tasks["Partner brief"]["agent_id"] == coord.id

        # 8. Failed work retried, then reassigned; rejected work retried with QA feedback.
        mkt = tasks["Case study outline"]
        assert mkt["reassigned"] is True
        events = [h["event"] for h in mkt["history"]]
        assert events.count("failed_attempt") == 2 and "reassigned" in events
        ops = s.scalars(select(Agent).where(Agent.role == "operations")).one()
        assert mkt["agent_id"] == ops.id
        fin = tasks["Budget check"]
        assert [h["verdict"] for h in fin["history"] if h["event"] == "qa"] == ["fail", "pass"]

        # 7. Every task was reviewed by QA.
        assert all(any(h["event"] == "qa" for h in t["history"]) for t in tasks.values())

        # 9. CRM updated by the agents and by the report.
        deal = s.get(Deal, deal_id)
        assert deal.stage == "negotiation"
        summaries = [a.summary for a in s.scalars(select(Activity).where(Activity.deal_id == deal_id))]
        assert any("Discovery call" in x for x in summaries)
        assert any("Objective 'Win the Northwind depot review' completed" in x for x in summaries)

        # Confidentiality: the partner brief names the client only by code.
        brief = s.scalars(select(Document).where(Document.kind == "partner_brief")).one()
        for ident in ["Northwind", "maria@northwind.io", "Maria Lopez", "northwind.io"]:
            assert ident.lower() not in brief.markdown.lower() and ident.lower() not in brief.html.lower()
        assert "Client-" in brief.markdown

        # 10. Executive report.
        report = s.get(Document, result["report_document_id"])
        assert report.kind == "executive_report"
        for text in ["Partner Coordinator", "Advance the deal", "(reassigned)", "AI spend"]:
            assert text in report.markdown
    # No human involvement between objective and report: nothing was sent and nothing awaits approval
    # except what the policy reserves for the owner.
    assert qa.run().passed, qa.run().render()


def test_partner_acquisition_outreach_is_owner_approved_and_prospects_get_no_work(tenant):
    """Agents may approach a prospective CA/CS firm, but every message waits for the owner (with
    preview), no work brief can go to an unconfirmed partner, and agents can never confirm one."""
    from wwos import approvals, comms, crm, partners, workflows
    from wwos.agents.runtime import execute_tool, find_agent
    from wwos.models import Tenant
    from wwos.tools import ToolContext

    tid, key = tenant
    with tenant_session(tid) as s:
        sales = ToolContext(s, s.get(Tenant, tid), find_agent(s, role="sales"))
        reg, err = execute_tool(sales, "partners.register", {
            "name": "Sharma & Co Chartered Accountants", "region": "india", "category": "ca_firm",
            "contact_email": "partners@sharmaco.example", "services": "statutory audit, GST"})
        assert not err and reg["status"] == "prospect" and reg["stage"] == "identified"
        pid = reg["partner_id"]
        out, err = execute_tool(sales, "partners.update_stage", {"partner_id": pid, "stage": "contacted"})
        assert not err
        out, err = execute_tool(sales, "partners.update_stage", {"partner_id": pid, "stage": "confirmed"})
        assert err and "reserved for the owner" in out["error"]
        draft, _ = execute_tool(sales, "comms.draft_email", {
            "idempotency_key": "intro", "to": "partners@sharmaco.example", "subject": "Partnership enquiry",
            "body": "We are exploring a referral partnership with CA firms."})
        sent, _ = execute_tool(sales, "comms.send_email", {"message_id": draft["message_id"]})
        assert sent["status"] == "pending_owner_approval" and sent["category"] == "partner_outreach"
        # No work can be shared with a prospect, even with an NDA on file.
        partners.attest_nda(s, pid, on_file=True, actor="owner")
        c, _ = crm.upsert_contact(s, email="x@client.example", first_name="X")
        deal = crm.create_deal(s, contact_id=c.id, title="Audit support", value="100")
        with pytest.raises(partners.PartnerError, match="not confirmed"):
            partners.prepare_brief(s, partner_id=pid, deal_id=deal.id, scope="Audit support work.", actor="CEO")
        req = s.scalars(select(ApprovalRequest)).one()
        approvals.decide(s, req.id, approve=True, actor="owner")
    workflows.drain()
    assert len(comms.OutboxTransport.delivered) == 1  # the approved introduction, nothing else
    with TestClient(app) as cl:
        listing = cl.get("/api/partners", headers=H(key)).json()[0]
        assert listing["status"] == "prospect" and listing["category"] == "ca_firm"
        assert listing["credentials_verified_by"] is None  # stated category, not verified
        bad = cl.post(f"/api/partners/{pid}/confirm", json={"agreement_on_file": False}, headers=H(key))
        assert bad.status_code == 400
        ok = cl.post(f"/api/partners/{pid}/confirm", json={"agreement_on_file": True, "credentials_verified": True},
                     headers=H(key)).json()
        assert ok["status"] == "confirmed"


def test_ca_cs_categories_only_for_indian_partners(tenant):
    from wwos import partners

    tid, _ = tenant
    with tenant_session(tid) as s:
        with pytest.raises(partners.PartnerError, match="Indian"):
            partners.register(s, name="X", region="international", category="ca_firm",
                              contact_email="a@x.example", actor="owner")


def test_operator_cannot_attest_nda_or_start_objectives(tenant):
    from wwos.bootstrap import create_api_key

    tid, key = tenant
    op = create_api_key(tid, "op", "operator")
    with TestClient(app) as c:
        p = c.post("/api/partners", headers=H(key), json={"name": "X Ltd", "region": "india",
                                                          "contact_email": "a@x-ltd.example"}).json()
        assert c.post(f"/api/partners/{p['id']}/nda", json={"on_file": True}, headers=H(op)).status_code == 403
        assert c.post("/api/objectives", json={"title": "t", "description": "d" * 30}, headers=H(op)).status_code == 403


def test_objective_with_no_plan_fails_cleanly_with_report(tenant):
    tid, key = tenant
    with TestClient(app) as c:
        obj = c.post("/api/objectives", headers=H(key), json={
            "title": "Nothing", "description": "An objective the (offline) CEO will not plan at all."}).json()
        workflows.drain()
        r = c.get(f"/api/objectives/{obj['id']}", headers=H(key)).json()
    assert r["status"] == "failed" and "no tasks" in r["failure_reason"] and r["report_document_id"]
