"""Acceptance tests owned by the QA function. They exercise the system only
through its public surfaces (HTTP API, worker, CLI-level functions), never by
reaching into service internals, and finish every scenario with the
independent QA invariant scan."""
import json

import pytest
from fastapi.testclient import TestClient

from wwos import comms, qa
from wwos.api import app

pytestmark = pytest.mark.acceptance

LEAD = {"email": "jane@acme.com", "first_name": "Jane", "last_name": "Doe", "company": "Acme Ltd",
        "need": "We need help redesigning our operations model for growth", "estimated_value": "25000"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def H(key):
    return {"Authorization": f"Bearer {key}"}


def assert_qa_clean():
    report = qa.run()
    assert report.passed, report.render()


def test_health_and_authentication(client, tenant):
    _, key = tenant
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/api/agents").status_code == 401
    assert client.get("/api/agents", headers=H("wwos_bogus")).status_code == 401
    assert client.get("/api/agents", headers=H(key)).status_code == 200


def test_role_based_access(client, tenant):
    from wwos.bootstrap import create_api_key

    tid, _ = tenant
    viewer = create_api_key(tid, "v", "viewer")
    operator = create_api_key(tid, "o", "operator")
    assert client.post("/api/leads", json=LEAD, headers=H(viewer)).status_code == 403
    assert client.post("/api/leads", json=LEAD, headers=H(operator)).status_code == 202
    agent_spec = {"name": "X", "role": "x", "department_code": "sales", "tools": [],
                  "instructions": "A" * 50, "monthly_budget_usd": "1"}
    assert client.post("/api/agents", json=agent_spec, headers=H(operator)).status_code == 403
    assert client.get("/api/audit", headers=H(operator)).status_code == 403


def test_full_business_cycle_lead_to_sent_proposal(client, tenant):
    _, key = tenant
    r = client.post("/api/leads", json=LEAD, headers=H(key))
    assert r.status_code == 202
    assert client.post("/api/worker/drain", headers=H(key)).json()["processed"] == 4

    deals = client.get("/api/deals", headers=H(key)).json()
    assert len(deals) == 1 and deals[0]["stage"] == "proposal"
    docs = client.get("/api/documents", headers=H(key)).json()
    assert [d["kind"] for d in docs] == ["proposal"]
    html = client.get(f"/api/documents/{docs[0]['id']}", headers=H(key)).text
    assert "Acme Ltd" in html and "25,000.00" in html

    pending = client.get("/api/approvals", headers=H(key)).json()
    assert len(pending) == 1 and pending[0]["category"] == "external_communication"
    assert comms.OutboxTransport.delivered == []  # nothing leaves before the owner approves

    r = client.post(f"/api/approvals/{pending[0]['id']}/decision", json={"approve": True}, headers=H(key))
    assert r.json()["status"] == "approved"
    client.post("/api/worker/drain", headers=H(key))
    msgs = client.get("/api/messages", headers=H(key)).json()
    assert msgs[0]["status"] == "sent"
    assert len(comms.OutboxTransport.delivered) == 1
    assert comms.OutboxTransport.delivered[0]["to"] == "jane@acme.com"
    executed = client.get("/api/approvals?status=executed", headers=H(key)).json()
    assert len(executed) == 1

    activity = client.get(f"/api/deals/{deals[0]['id']}/activities", headers=H(key)).json()
    kinds = [a["kind"] for a in activity]
    assert "stage_change" in kinds and "document" in kinds and "note" in kinds

    # Advance the deal to won and invoice it.
    for stage in ["negotiation", "won"]:
        assert client.post(f"/api/deals/{deals[0]['id']}/stage", json={"stage": stage}, headers=H(key)).status_code == 200
    inv = client.post("/api/documents", headers=H(key), json={
        "kind": "invoice", "title": "Invoice INV-1", "deal_id": deals[0]["id"],
        "data": {"client_name": "Acme Ltd", "invoice_number": "INV-1", "currency": "USD", "due_days": 30,
                 "line_items": [{"description": "Phase 1", "quantity": 1, "unit_price": "25000"}]}})
    assert inv.status_code == 201
    assert_qa_clean()


def test_rejected_email_is_never_sent(client, tenant):
    _, key = tenant
    client.post("/api/leads", json=LEAD, headers=H(key))
    client.post("/api/worker/drain", headers=H(key))
    pending = client.get("/api/approvals", headers=H(key)).json()
    client.post(f"/api/approvals/{pending[0]['id']}/decision", json={"approve": False}, headers=H(key))
    client.post("/api/worker/drain", headers=H(key))
    assert client.get("/api/messages", headers=H(key)).json()[0]["status"] == "rejected"
    assert comms.OutboxTransport.delivered == []
    # Approvals cannot be decided twice.
    r = client.post(f"/api/approvals/{pending[0]['id']}/decision", json={"approve": True}, headers=H(key))
    assert r.status_code == 400
    assert_qa_clean()


def test_cross_tenant_api_access_is_impossible(client, tenant, other_tenant):
    _, key_a = tenant
    _, key_b = other_tenant
    client.post("/api/leads", json=LEAD, headers=H(key_a))
    client.post("/api/worker/drain", headers=H(key_a))
    deal_id = client.get("/api/deals", headers=H(key_a)).json()[0]["id"]
    doc_id = client.get("/api/documents", headers=H(key_a)).json()[0]["id"]
    assert client.get("/api/deals", headers=H(key_b)).json() == []
    assert client.get(f"/api/documents/{doc_id}", headers=H(key_b)).status_code == 404
    assert client.post(f"/api/deals/{deal_id}/stage", json={"stage": "lost"}, headers=H(key_b)).status_code == 404
    assert client.get(f"/api/deals/{deal_id}/activities", headers=H(key_b)).status_code == 404
    assert client.get("/api/deals", headers=H(key_a)).json()[0]["stage"] == "proposal"
    assert_qa_clean()


def test_ceo_agent_dynamically_creates_and_delegates_to_a_specialist(client, tenant):
    """Dynamic agent creation by an agent, end to end, with no human involvement.
    The offline provider turns `TOOL:` lines into real tool calls through the full runtime."""
    _, key = tenant
    agents = {a["role"]: a for a in client.get("/api/agents", headers=H(key)).json()}
    spec = {"name": "Proposal Analyst", "role": "specialist", "department_code": "sales",
            "instructions": "You review pipeline deals and report which proposals need follow-up this week.",
            "tools": ["deals.get"], "monthly_budget_usd": 10}
    instruction = (f"TOOL: agents.provision {json.dumps(spec)}\n"
                   f"TOOL: budget.status {{}}")
    r = client.post("/api/tasks", json={"agent_id": agents["ceo"]["id"], "instruction": instruction}, headers=H(key))
    assert r.status_code == 202
    client.post("/api/worker/drain", headers=H(key))
    task = client.get("/api/tasks", headers=H(key)).json()[0]
    assert task["status"] == "succeeded", task["last_error"]
    assert [c["error"] for c in task["result"]["tool_calls"]] == [False, False]

    new = [a for a in client.get("/api/agents", headers=H(key)).json() if a["name"] == "Proposal Analyst"]
    assert len(new) == 1 and new[0]["status"] == "active" and new[0]["created_by"] == "CEO"

    # Escalation attempt: the CEO cannot mint an agent with powers it lacks.
    bad = {**spec, "name": "Paymaster", "tools": ["finance.transfer_funds"]}
    client.post("/api/tasks", headers=H(key), json={"agent_id": agents["ceo"]["id"],
                                                    "instruction": f"TOOL: agents.provision {json.dumps(bad)}"})
    client.post("/api/worker/drain", headers=H(key))
    assert not [a for a in client.get("/api/agents", headers=H(key)).json() if a["name"] == "Paymaster"]
    assert_qa_clean()


def test_owner_manages_agent_lifecycle_via_api(client, tenant):
    _, key = tenant
    sales = next(a for a in client.get("/api/agents", headers=H(key)).json() if a["role"] == "sales")
    assert client.post(f"/api/agents/{sales['id']}/suspend", json={"reason": "review"}, headers=H(key)).json()["status"] == "suspended"
    assert client.post("/api/tasks", json={"agent_id": sales["id"], "instruction": "hello"}, headers=H(key)).status_code == 409
    assert client.post(f"/api/agents/{sales['id']}/reactivate", headers=H(key)).json()["status"] == "active"
    v2 = client.post(f"/api/agents/{sales['id']}/revise", headers=H(key),
                     json={"model": "claude-sonnet-5-5"}).json()
    assert v2["version"] == 2 and v2["model"] == "claude-sonnet-5-5" and v2["status"] == "active"
    bad = client.post(f"/api/agents/{v2['id']}/revise", headers=H(key), json={"model": "gpt-4o"})
    assert bad.status_code == 400
    assert_qa_clean()


def test_dashboard_login_and_approval_from_dashboard(client, tenant):
    _, key = tenant
    assert client.get("/dashboard", follow_redirects=False).status_code in (302, 307)
    client.post("/api/leads", json=LEAD, headers=H(key))
    client.post("/api/worker/drain", headers=H(key))
    r = client.post("/login", data={"api_key": key}, follow_redirects=False)
    assert r.status_code == 303 and "wwos_key" in r.cookies
    page = client.get("/dashboard")
    assert page.status_code == 200
    for text in ["Owner approvals", "Agent workforce", "Pipeline", "CEO", "comms.send_email"]:
        assert text in page.text
    approval_id = client.get("/api/approvals", headers=H(key)).json()[0]["id"]
    r = client.post(f"/dashboard/approvals/{approval_id}", data={"decision": "approve"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/approvals", headers=H(key)).json() == []
    bad = client.post("/login", data={"api_key": "nope"}, follow_redirects=False)
    assert "error" in bad.headers["location"]


def test_backup_and_restore_round_trip(client, tenant, tmp_path, fresh_db):
    from wwos import backup, db

    _, key = tenant
    client.post("/api/leads", json=LEAD, headers=H(key))
    client.post("/api/worker/drain", headers=H(key))
    path = backup.backup(str(tmp_path / "bk"), url=fresh_db)
    before = backup.table_counts(fresh_db)
    # Simulate data loss, then restore.
    client.post("/api/contacts", headers=H(key), json={"email": "late@x.com", "first_name": "Late"})
    assert backup.table_counts(fresh_db)["contacts"] == before["contacts"] + 1
    db.engine().dispose()
    restored = backup.restore(path, url=fresh_db)
    assert restored == before
    assert len(client.get("/api/contacts", headers=H(key)).json()) == before["contacts"]
    assert_qa_clean()


def test_qa_detects_injected_violations(tenant):
    """QA must catch problems, not just pass: inject defects directly and expect failures."""
    from decimal import Decimal

    from sqlalchemy import select

    from wwos.db import system_session
    from wwos.models import Agent, Deal

    with system_session() as s:
        a = s.scalars(select(Agent).where(Agent.role == "marketing")).one()
        a.tools = list(a.tools) + ["agents.provision"]  # privilege escalation
        a.monthly_budget_usd = Decimal("10000")  # over-allocation
    report = qa.run()
    failed = {c.name for c in report.checks if not c.passed}
    assert "Agent permissions: privilege containment holds" in failed
    assert "Budget: no tenant or agent over its ceiling" in failed
