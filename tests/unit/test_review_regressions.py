"""Regression tests for every defect confirmed by the independent QA review (docs/PROJECT_STATE.md)."""
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from wwos import approvals, comms, crm, workflows
from wwos.agents import lifecycle
from wwos.agents.runtime import execute_tool, find_agent
from wwos.api import app
from wwos.budget import spent_this_month
from wwos.db import system_session, tenant_session
from wwos.models import Agent, ApprovalRequest, Contact, Deal, Task, Tenant, utcnow
from wwos.policy import PermissionDenied
from wwos.tools import ToolContext

INSTR = "You review pipeline deals and report which proposals need follow-up this week."
LEAD = {"email": "jane@acme.com", "first_name": "Jane", "company": "Acme Ltd",
        "need": "We need help redesigning our operations model for growth", "estimated_value": "25000"}


def ctx(s, tid, role):
    return ToolContext(session=s, tenant=s.get(Tenant, tid), agent=find_agent(s, role=role))


# 1. CRITICAL: hidden kwargs injection into agents.provision
@pytest.mark.parametrize("extra", [{"baseline_tools": ["finance.transfer_funds"]}, {"version": 999},
                                   {"activate": False}, {"replacing_agent_id": "x"}, {"lineage_id": "x"},
                                   {"can_provision": True}, {"creator": None}])
def test_tool_args_cannot_inject_internal_parameters(tenant, extra):
    tid, _ = tenant
    with tenant_session(tid) as s:
        before = len(s.scalars(select(Agent)).all())
        spec = {"name": "Evil", "role": "sales", "department_code": "sales", "instructions": INSTR,
                "tools": ["deals.get"], "monthly_budget_usd": 5, **extra}
        out, err = execute_tool(ctx(s, tid, "ceo"), "agents.provision", spec)
        assert err and "unexpected arguments" in out["error"]
        assert len(s.scalars(select(Agent)).all()) == before


def test_tool_args_type_checked(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        out, err = execute_tool(ctx(s, tid, "ceo"), "agents.provision",
                                {"name": "X", "role": "x", "department_code": "sales", "instructions": INSTR,
                                 "tools": "finance.transfer_funds"})
        assert err and "wrong type" in out["error"]


# 2. HIGH: retries must not resend email
def test_retried_step_never_sends_email_twice(tenant, monkeypatch):
    tid, _ = tenant
    monkeypatch.setenv("WWOS_EXTERNAL_EMAIL_REQUIRES_APPROVAL", "false")
    original = workflows.HANDLERS["workflow.outreach"]
    calls = {"n": 0}

    def flaky(s, tenant_obj, task, trace):
        result = original(s, tenant_obj, task, trace)
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient failure after queuing the email")
        return result

    monkeypatch.setitem(workflows.HANDLERS, "workflow.outreach", flaky)
    with tenant_session(tid) as s:
        workflows.start_lead_workflow(s, LEAD)
    for _ in range(6):
        with system_session() as s:
            s.execute(update(Task).where(Task.status == "queued").values(run_after=utcnow() - timedelta(seconds=1)))
        workflows.drain()
    assert calls["n"] == 3
    assert len(comms.OutboxTransport.delivered) == 1


# 3a. approvals must always be executable
def test_unexpected_args_rejected_before_approval_is_filed(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        out, err = execute_tool(ctx(s, tid, "legal"), "legal.sign_agreement",
                                {"document_id": "d", "counterparty": "c", "justification": "pls"})
        assert err and s.scalars(select(ApprovalRequest)).all() == []
        out, err = execute_tool(ctx(s, tid, "legal"), "legal.sign_agreement", {"document_id": "d", "counterparty": "c"})
        approvals.decide(s, out["approval_id"], approve=True, actor="owner")
    workflows.drain()
    with tenant_session(tid) as s:
        assert s.scalars(select(ApprovalRequest)).one().status == "executed"


# 3b. key squatting
def test_agent_keys_are_namespaced_and_kinds_cannot_alias(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        legal = ctx(s, tid, "legal")
        out, _ = execute_tool(legal, "legal.sign_agreement", {"document_id": "d", "counterparty": "c"})
        approval_id = out["approval_id"]
        ops = ctx(s, tid, "operations")
        sales = find_agent(s, role="sales")
        res, err = execute_tool(ops, "tasks.delegate", {"agent_id": sales.id, "instruction": "x",
                                                        "idempotency_key": f"approval:{approval_id}"})
        assert not err
        assert s.get(Task, res["task_id"]).idempotency_key.startswith("agent:")
        approvals.decide(s, approval_id, approve=True, actor="owner")
        with pytest.raises(PermissionDenied):
            workflows.enqueue(s, kind="approval.execute", idempotency_key=s.get(Task, res["task_id"]).idempotency_key)
    workflows.drain()
    with tenant_session(tid) as s:
        assert s.get(ApprovalRequest, approval_id).status == "executed"


# 4. HIGH: acceptance-test spend must survive the enclosing run's failure
def test_nested_acceptance_spend_survives_rollback(tenant, scripted, monkeypatch):
    tid, _ = tenant
    monkeypatch.setenv("WWOS_TASK_MAX_ATTEMPTS", "1")
    spec = {"name": "Analyst", "role": "specialist", "department_code": "sales", "instructions": INSTR,
            "tools": ["deals.get"], "monthly_budget_usd": 5}
    p = scripted(input_tokens=1000, output_tokens=1000,
                 script=[[{"type": "tool_use", "id": "t1", "name": "agents__provision", "input": spec}],
                         [{"type": "text", "text": "I am the analyst."}]])
    orig = p.complete

    def third_call_crashes(**kw):
        if p.calls >= 2:
            p.calls += 1
            raise RuntimeError("crash")
        return orig(**kw)
    p.complete = third_call_crashes
    with tenant_session(tid) as s:
        workflows.enqueue(s, kind="agent_run", idempotency_key="k", agent_id=find_agent(s, role="ceo").id,
                          payload={"instruction": "go"})
    workflows.drain()
    with tenant_session(tid) as s:
        assert spent_this_month(s) == Decimal("0.048")  # both paid calls (CEO turn + acceptance test)
        assert not s.scalars(select(Agent).where(Agent.name == "Analyst")).all()


def test_failed_reactivation_keeps_its_spend(tenant, scripted):
    tid, key = tenant
    with tenant_session(tid) as s:
        sales = find_agent(s, role="sales")
        lifecycle.suspend(s, sales, "x", "owner")
        sid = sales.id
    scripted(input_tokens=1000, output_tokens=1000, script=[[{"type": "text", "text": ""}]])
    with TestClient(app) as c:
        r = c.post(f"/api/agents/{sid}/reactivate", headers={"Authorization": f"Bearer {key}"})
        assert r.status_code == 409 and r.json()["status"] == "suspended"
    with tenant_session(tid) as s:
        assert spent_this_month(s) == Decimal("0.024")


# 5. MEDIUM: a reclaimed task cannot commit twice
def test_stale_worker_cannot_commit_after_reclaim(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        workflows.enqueue(s, kind="workflow.lead_intake", idempotency_key="lease", payload=LEAD, workflow_id="wf")
    first = workflows.claim_next()
    with system_session() as s:
        s.execute(update(Task).values(lease_until=utcnow() - timedelta(seconds=1)))
    second = workflows.claim_next()
    assert first[0] == second[0] and first[2] != second[2]
    assert workflows.process(*second) == "succeeded"
    assert workflows.process(*first) == "lease_lost"
    with tenant_session(tid) as s:
        assert len(s.scalars(select(Deal)).all()) == 1


# 6. operators cannot borrow the CEO's authority; delegation cannot launder it
def test_operator_cannot_task_provisioning_agents(tenant):
    from wwos.bootstrap import create_api_key

    tid, _ = tenant
    op = create_api_key(tid, "op", "operator")
    with tenant_session(tid) as s:
        ceo_id = find_agent(s, role="ceo").id
        out, err = execute_tool(ctx(s, tid, "operations"), "tasks.delegate", {"agent_id": ceo_id, "instruction": "x"})
        assert err and "owner" in out["error"]
    with TestClient(app) as c:
        r = c.post("/api/tasks", json={"agent_id": ceo_id, "instruction": "provision"},
                   headers={"Authorization": f"Bearer {op}"})
        assert r.status_code == 403


# 7. referenced ids must belong to the caller's tenant
def test_foreign_ids_rejected_in_tool_args(tenant, other_tenant):
    tid, _ = tenant
    oid, _ = other_tenant
    with tenant_session(oid) as s:
        foreign, _ = crm.upsert_contact(s, email="f@o.com", first_name="F")
        fid = foreign.id
    with tenant_session(tid) as s:
        out, err = execute_tool(ctx(s, tid, "sales"), "crm.log_activity", {"kind": "note", "summary": "x",
                                                                          "contact_id": fid})
        assert err and "not found" in out["error"]
        out, err = execute_tool(ctx(s, tid, "sales"), "comms.draft_email",
                                {"idempotency_key": "k", "to": "a@b.com", "subject": "s", "body": "b",
                                 "contact_id": fid})
        assert err and "not found" in out["error"]


# 8. delegation loops
def test_self_delegation_and_depth_limit(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        ops = ctx(s, tid, "operations")
        out, err = execute_tool(ops, "tasks.delegate", {"agent_id": ops.agent.id, "instruction": "loop"})
        assert err and "itself" in out["error"]
        ops.depth = 3
        out, err = execute_tool(ops, "tasks.delegate", {"agent_id": find_agent(s, role="sales").id,
                                                        "instruction": "deep"})
        assert err and "too deep" in out["error"]


# 9. drain is owner-only and tenant-scoped
def test_drain_is_owner_only_and_tenant_scoped(tenant, other_tenant):
    from wwos.bootstrap import create_api_key

    tid, key = tenant
    oid, _ = other_tenant
    op = create_api_key(tid, "op", "operator")
    with tenant_session(oid) as s:
        workflows.start_lead_workflow(s, LEAD)
    with TestClient(app) as c:
        assert c.post("/api/worker/drain", headers={"Authorization": f"Bearer {op}"}).status_code == 403
        assert c.post("/api/worker/drain", headers={"Authorization": f"Bearer {key}"}).json()["processed"] == 0
    with tenant_session(oid) as s:
        assert s.scalars(select(Task)).one().status == "queued"


# LOW findings
def test_tasks_for_revised_agents_follow_the_lineage(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        eng = find_agent(s, role="delivery")
        workflows.enqueue(s, kind="agent_run", idempotency_key="old", agent_id=eng.id, payload={"instruction": "x"})
        lifecycle.revise(s, s.get(Tenant, tid), eng, actor_agent=None, instructions=INSTR)
    workflows.drain()
    with tenant_session(tid) as s:
        assert s.scalars(select(Task)).one().status == "succeeded"


def test_repeat_enquiry_does_not_downgrade_customer(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        c, _ = crm.upsert_contact(s, email="vip@x.com", first_name="V")
        c.lifecycle = "customer"
        workflows.start_lead_workflow(s, {"email": "vip@x.com", "first_name": "V", "need": "hi"})
    workflows.drain()
    with tenant_session(tid) as s:
        assert s.scalars(select(Contact)).one().lifecycle == "customer"


def test_api_hardening(tenant):
    tid, key = tenant
    with TestClient(app) as c:
        h = {"Authorization": f"Bearer {key}"}
        assert c.get("/health").json() == {"status": "ok"}
        assert c.get("/health/details").status_code == 404
        assert c.get("/api/tasks?limit=-1", headers=h).status_code == 200
        r = c.post("/login", data={"api_key": key}, headers={"origin": "https://evil.example"},
                   follow_redirects=False)
        assert r.status_code == 403
    with system_session() as s:
        from wwos.bootstrap import hash_key
        from wwos.models import ApiKey
        s.add(ApiKey(tenant_id=tid, name="odd", key_hash=hash_key("wwos_odd"), role="superuser"))
    with TestClient(app) as c:
        assert c.get("/api/deals", headers={"Authorization": "Bearer wwos_odd"}).status_code == 403


def test_rejected_message_cannot_be_sent(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        m, _ = comms.draft_email(s, idempotency_key="r", to="a@b.com", subject="s", body="b")
        m.status = "rejected"
        with pytest.raises(ValueError):
            comms.request_send(s, m.id)


def test_provisioning_agent_auto_suspension_goes_to_owner_not_tech(tenant, scripted):
    tid, _ = tenant
    scripted(error=RuntimeError("boom"))
    with tenant_session(tid) as s:
        ceo = find_agent(s, role="ceo")
        for i in range(3):
            workflows.enqueue(s, kind="agent_run", idempotency_key=f"c{i}", agent_id=ceo.id,
                              payload={"instruction": "x"})
        cid = ceo.id
    workflows.drain(max_tasks=3)
    with tenant_session(tid) as s:
        assert s.get(Agent, cid).status == "suspended"
        assert not s.scalars(select(Task).where(Task.idempotency_key.like("recover:%"))).all()



# ---- Second verification round
def test_task_that_always_overruns_its_lease_is_dead_lettered(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        workflows.enqueue(s, kind="workflow.lead_intake", idempotency_key="overrun", payload=LEAD, workflow_id="w")
    claims = 0
    for _ in range(10):
        if workflows.claim_next() is None:
            break
        claims += 1
        with system_session() as s:
            s.execute(update(Task).values(lease_until=utcnow() - timedelta(seconds=1)))
    with tenant_session(tid) as s:
        t = s.scalars(select(Task)).one()
        assert t.status == "dead" and "lease expired" in t.last_error
    assert claims == 4


def test_origin_check_behind_tls_proxy(tenant, monkeypatch):
    _, key = tenant
    with TestClient(app) as c:
        # Proxy terminates TLS: browser origin is https, the app sees http on the same host.
        r = c.post("/login", data={"api_key": key}, headers={"origin": "https://testserver"},
                   follow_redirects=False)
        assert r.status_code == 303
    monkeypatch.setenv("WWOS_PUBLIC_ORIGIN", "https://ops.example.com")
    with TestClient(app) as c:
        ok = c.post("/login", data={"api_key": key}, headers={"origin": "https://ops.example.com"},
                    follow_redirects=False)
        bad = c.post("/login", data={"api_key": key}, headers={"origin": "https://testserver"},
                     follow_redirects=False)
        assert ok.status_code == 303 and bad.status_code == 403


def test_logout_revokes_session_and_cookie_writes_need_origin(tenant):
    _, key = tenant
    with TestClient(app) as c:
        c.post("/login", data={"api_key": key}, follow_redirects=False)
        assert c.get("/api/deals").status_code == 200
        r = c.post("/api/contacts", json={"email": "x@y.com", "first_name": "X"},
                   headers={"origin": "https://evil.example"})
        assert r.status_code == 403
        cookie = c.cookies.get("wwos_session")
        c.post("/logout", headers={"origin": "https://evil.example"}, follow_redirects=False)
        c.cookies.set("wwos_session", cookie)
        assert c.get("/api/deals").status_code == 401


def test_login_rate_limited(tenant):
    with TestClient(app) as c:
        codes = [c.post("/login", data={"api_key": "wrong"}, follow_redirects=False).status_code for _ in range(12)]
        assert codes[-1] == 429 and codes[0] == 303


@pytest.mark.parametrize("amount", ["NaN", "-1e9", "0", "Infinity", "abc"])
def test_amounts_must_be_finite_and_positive(tenant, amount):
    tid, key = tenant
    with tenant_session(tid) as s:
        out, err = execute_tool(ctx(s, tid, "finance"), "finance.transfer_funds",
                                {"amount": amount, "currency": "USD", "payee": "X"})
        assert err and s.scalars(select(ApprovalRequest)).all() == []
    with TestClient(app) as c:
        r = c.post("/api/documents", headers={"Authorization": f"Bearer {key}"}, json={
            "kind": "invoice", "title": "t", "data": {"client_name": "A", "invoice_number": "1", "currency": "USD",
                                                     "due_days": 30, "line_items": [{"description": "d",
                                                                                     "unit_price": amount}]}})
        assert r.status_code == 400 if amount != "0" else r.status_code == 201


def test_email_retries_back_off(tenant):
    tid, _ = tenant

    class Boom:
        def send(self, **kw):
            raise comms.DeliveryRejected("down", retryable=True)

    with tenant_session(tid) as s:
        m, _ = comms.draft_email(s, idempotency_key="b", to="a@b.com", subject="s", body="b")
        comms.request_send(s, m.id)
    comms.set_transport(Boom())
    for _ in range(5):
        comms.dispatch()
    with tenant_session(tid) as s:
        assert s.get(type(m), m.id).attempts == 1  # later attempts wait for the backoff window


def test_delegation_breadth_limited(tenant):
    from wwos.agents.runtime import RunTrace

    tid, _ = tenant
    with tenant_session(tid) as s:
        ops = ctx(s, tid, "operations")
        ops.trace = RunTrace()
        sales = find_agent(s, role="sales")
        results = []
        for i in range(7):
            out, err = execute_tool(ops, "tasks.delegate", {"agent_id": sales.id, "instruction": f"job {i}"}, ops.trace)
            ops.trace.tool_calls.append({"tool": "tasks.delegate", "error": err})
            results.append(err)
        assert results == [False] * 5 + [True] * 2


# ---- Security review round 3
def test_revision_does_not_reset_agent_spend(tenant, scripted):
    from wwos.agents.runtime import run_agent_prompt
    from wwos.budget import BudgetExceeded

    tid, _ = tenant
    scripted(input_tokens=10, output_tokens=100_000)  # $2 per call
    with tenant_session(tid) as s:
        qa_agent = find_agent(s, role="qa")
        qa_agent.monthly_budget_usd = Decimal("5")
    with tenant_session(tid) as s:
        a = find_agent(s, role="qa")
        run_agent_prompt(s, s.get(Tenant, tid), a, "one", max_tokens=100_000)
        run_agent_prompt(s, s.get(Tenant, tid), a, "two", max_tokens=100_000)
    with tenant_session(tid) as s:  # new version, same lineage: $4 already spent this month
        new = lifecycle.revise(s, s.get(Tenant, tid), find_agent(s, role="qa"), actor_agent=None,
                               instructions=INSTR + " Revised.")
        assert new.version == 2
    with tenant_session(tid) as s:
        with pytest.raises(BudgetExceeded):
            run_agent_prompt(s, s.get(Tenant, tid), find_agent(s, role="qa"), "three", max_tokens=100_000)


def test_lead_keys_cannot_squat_schedule_keys(tenant, other_tenant):
    from datetime import datetime, timezone

    from wwos import schedule

    tid, key = tenant
    oid, _ = other_tenant
    day = datetime(2026, 12, 1, 9, tzinfo=timezone.utc)
    with TestClient(app) as c:
        r = c.post("/api/leads", headers={"Authorization": f"Bearer {key}"},
                   json={**LEAD, "idempotency_key": "sched:ceo-daily-review:2026-12-01"})
        assert r.status_code == 202
    assert schedule.tick(day) == 4  # both tenants' two jobs still scheduled
    with tenant_session(oid) as s:
        assert len(s.scalars(select(Task).where(Task.idempotency_key.like("sched:%"))).all()) == 2


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e30", "-1", "abc"])
def test_deal_values_validated(tenant, value):
    tid, key = tenant
    with tenant_session(tid) as s:
        cid = crm.upsert_contact(s, email="v@x.com", first_name="V")[0].id
    with TestClient(app) as c:
        r = c.post("/api/deals", headers={"Authorization": f"Bearer {key}"},
                   json={"contact_id": cid, "title": "t", "value": value})
        assert r.status_code == 400


def test_bad_invoice_line_items_are_400(tenant):
    _, key = tenant
    with TestClient(app) as c:
        r = c.post("/api/documents", headers={"Authorization": f"Bearer {key}"}, json={
            "kind": "invoice", "title": "t", "data": {"client_name": "A", "invoice_number": "1", "currency": "USD",
                                                     "due_days": 30, "line_items": ["x"]}})
        assert r.status_code == 400
        r = c.post("/api/documents", headers={"Authorization": f"Bearer {key}"}, json={
            "kind": "proposal", "title": "t", "data": {"client_name": "A", "objective": "o", "scope_items": ["s"],
                                                      "fee": "1e30", "currency": "USD", "valid_days": 30}})
        assert r.status_code == 400


def test_no_tools_offered_means_no_tools_executed(tenant):
    from wwos.agents.runtime import run_agent_prompt

    tid, _ = tenant
    with tenant_session(tid) as s:  # offline provider turns TOOL: lines into calls
        text = run_agent_prompt(s, s.get(Tenant, tid), find_agent(s, role="sales"),
                                'TOOL: crm.upsert_contact {"email": "inj@x.com", "first_name": "I"}', allow_tools=False)
        assert s.scalars(select(Contact)).all() == []


def test_agents_cannot_claim_founding_roles(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        with pytest.raises(lifecycle.ProvisioningError, match="reserved"):
            lifecycle.provision(s, s.get(Tenant, tid), name="Fake QA", role="qa", department_code="qa",
                                instructions=INSTR, tools=["deals.get"], creator=find_agent(s, role="ceo"))


def test_security_headers_and_attribution(tenant):
    from wwos.models import AuditLog, Activity

    tid, key = tenant
    with TestClient(app) as c:
        r = c.post("/api/contacts", headers={"Authorization": f"Bearer {key}"}, json={"email": "z@z.com", "first_name": "Z"})
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    with tenant_session(tid) as s:
        assert s.scalars(select(Activity)).one().actor == "api:owner-bootstrap"


def test_successful_logins_do_not_count_towards_lockout(tenant):
    _, key = tenant
    with TestClient(app) as c:
        codes = [c.post("/login", data={"api_key": key}, follow_redirects=False).status_code for _ in range(15)]
        assert set(codes) == {303}


def test_agents_cannot_raise_owner_set_budgets_or_spoof_role_case(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        t = s.get(Tenant, tid)
        tech = find_agent(s, role="technology")
        with pytest.raises(lifecycle.ProvisioningError, match="owner-set"):
            lifecycle.revise(s, t, find_agent(s, role="sales"), actor_agent=tech, monthly_budget_usd="100")
        with pytest.raises(lifecycle.ProvisioningError, match="reserved"):
            lifecycle.provision(s, t, name="Boss", role=" CEO ", department_code="executive", instructions=INSTR,
                                tools=["deals.get"], creator=find_agent(s, role="ceo"))
        # Re-allocation among agent-created specialists is still allowed (resource management).
        spec = lifecycle.provision(s, t, name="Analyst", role="specialist", department_code="sales",
                                   instructions=INSTR, tools=["deals.get"], creator=find_agent(s, role="ceo"),
                                   monthly_budget_usd="5")
        new = lifecycle.revise(s, t, spec, actor_agent=tech, monthly_budget_usd="10")
        assert new.status == "active" and new.monthly_budget_usd == Decimal("10")
