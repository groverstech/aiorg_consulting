"""Regression tests for security review round 5 (Agent SDK runtime and partner pipeline)."""
import json
import socket
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from sqlalchemy import select

from wwos import budget_guard, comms, crm, partners
from wwos.agents import budget_proxy
from wwos.db import tenant_session
from wwos.models import BudgetReservation, OutboundMessage

MODEL = "claude-opus-5-5"


def _start(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _run(tid, monkeypatch, upstream, cap=4000, budget="100"):
    monkeypatch.setenv("WWOS_ANTHROPIC_UPSTREAM", upstream)
    run = budget_proxy.RunBudget(tenant_id=tid, agent_id="agent-x", agent_name="x", agent_budget=Decimal(budget),
                                 lineage_ids=["agent-x"], task_id=None, model=MODEL, max_output_tokens=cap)
    token = budget_proxy.register(run)
    return run, f"http://127.0.0.1:{budget_proxy.ensure_started()}/run/{token}"


class DropAfterStart(BaseHTTPRequestHandler):
    """Upstream that bills (sends message_start with large input) then drops the connection."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        start = {"type": "message_start", "message": {"usage": {"input_tokens": 150000, "output_tokens": 1}}}
        self.wfile.write(f"event: message_start\ndata: {json.dumps(start)}\n\n".encode())
        self.wfile.flush()
        self.connection.shutdown(socket.SHUT_RDWR)


class SlowFullStream(BaseHTTPRequestHandler):
    """Upstream that streams a complete response slowly (so the client can disconnect midway)."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen_headers = dict(self.headers)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")  # stream ends when the connection closes
        self.end_headers()
        self.close_connection = True
        start = {"type": "message_start", "message": {"usage": {"input_tokens": 1000, "output_tokens": 1,
                                                                "cache_creation_input_tokens": 1000}}}
        self.wfile.write(f"event: message_start\ndata: {json.dumps(start)}\n\n".encode())
        self.wfile.flush()
        time.sleep(0.5)
        delta = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 500}}
        self.wfile.write(f"event: message_delta\ndata: {json.dumps(delta)}\n\n".encode())
        self.wfile.flush()


def _status(rid):
    from wwos.db import system_session

    with system_session() as s:
        r = s.get(BudgetReservation, rid)
        return (r.status, r.input_tokens, r.output_tokens) if r else None


def _incurred(run, rid):
    """Memory store (SQLite) keeps reservations in process."""
    store = budget_guard.store()
    res = store._res.get(rid) if hasattr(store, "_res") else None
    return (res.status, res.input_tokens, res.output_tokens) if res else _status(rid)


def test_H1_upstream_drop_after_billing_is_recorded_at_worst_case(tenant, monkeypatch):
    tid, _ = tenant
    srv = _start(DropAfterStart)
    run, url = _run(tid, monkeypatch, f"http://127.0.0.1:{srv.server_address[1]}")
    try:
        httpx.post(f"{url}/v1/messages", json={"model": MODEL, "max_tokens": 128000, "stream": True,
                                                "messages": [{"role": "user", "content": "hi"}]}, timeout=30)
    except httpx.HTTPError:
        pass
    time.sleep(0.5)
    [rid] = run.reservations
    status, inp, out = _incurred(run, rid)
    assert status == "incurred" and inp == 150000 and out == 4000  # missing output priced at the cap
    srv.shutdown()


def test_H1_client_disconnect_still_drains_and_records_usage(tenant, monkeypatch):
    tid, _ = tenant
    srv = _start(SlowFullStream)
    run, url = _run(tid, monkeypatch, f"http://127.0.0.1:{srv.server_address[1]}")
    with httpx.Client(timeout=30) as c:
        with c.stream("POST", f"{url}/v1/messages", json={"model": MODEL, "max_tokens": 1000, "stream": True,
                                                          "messages": [{"role": "user", "content": "hi"}]}) as r:
            next(r.iter_bytes())  # read the first event, then hang up
    time.sleep(1.5)
    [rid] = run.reservations
    status, inp, out = _incurred(run, rid)
    # cache writes priced at 2x input: 1000 + 2*1000; real output from the drained stream
    assert status == "incurred" and inp == 3000 and out == 500
    srv.shutdown()


def test_M1_premium_betas_and_fast_mode_are_stripped(tenant, monkeypatch):
    tid, _ = tenant
    srv = _start(SlowFullStream)
    run, url = _run(tid, monkeypatch, f"http://127.0.0.1:{srv.server_address[1]}")
    httpx.post(f"{url}/v1/messages", headers={"anthropic-beta": "context-1m-2025-08-07,interleaved-thinking-2025-05-14"},
               json={"model": MODEL, "max_tokens": 100, "speed": "fast", "messages": [{"role": "user", "content": "x"}]},
               timeout=30)
    beta = srv.seen_headers.get("anthropic-beta", "")
    assert "context-1m" not in beta and "interleaved-thinking" in beta
    srv.shutdown()


def test_L1_count_tokens_needs_the_agent_model_and_is_capped(tenant, monkeypatch):
    tid, _ = tenant
    run, url = _run(tid, monkeypatch, "http://127.0.0.1:9")
    r = httpx.post(f"{url}/v1/messages/count_tokens", json={"model": "claude-fable-5-2", "messages": []})
    assert r.status_code == 400
    run.count_calls = budget_proxy.MAX_COUNT_TOKENS_PER_RUN
    r = httpx.post(f"{url}/v1/messages/count_tokens", json={"model": MODEL, "messages": []})
    assert r.status_code == 429


def test_L4_token_checked_before_body_and_size_capped(tenant, monkeypatch):
    tid, _ = tenant
    run, url = _run(tid, monkeypatch, "http://127.0.0.1:9")
    port = budget_proxy.ensure_started()
    s = socket.create_connection(("127.0.0.1", port))
    s.sendall(b"POST /run/forged/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 999999999\r\n\r\n")
    assert b" 403 " in s.recv(200)  # refused without waiting for the 1 GB body
    s.close()
    token = url.rsplit("/", 1)[1]
    s = socket.create_connection(("127.0.0.1", port))
    s.sendall(f"POST /run/{token}/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: {budget_proxy.MAX_BODY_BYTES + 1}\r\n\r\n".encode())
    assert b" 413 " in s.recv(200)  # refused before reading the body
    s.close()
    s = socket.create_connection(("127.0.0.1", port))
    s.sendall(f"POST /run/{token}/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: -5\r\n\r\n".encode())
    assert b" 413 " in s.recv(200)
    s.close()


@pytest.fixture
def partner_setup(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        c, _ = crm.upsert_contact(s, email="ceo@zenith-holdings.example", first_name="Priya", last_name="Raman",
                                  company="Zenith Holdings")
        crm.create_deal(s, contact_id=c.id, title="Zenith review", value="100")
        firm = partners.register(s, name="Firm & Co CA", region="india", category="ca_firm",
                                 contact_email="ops@firmco.example", actor="owner")
        return tid, firm.id


def test_M2_overlapping_partner_domains_refused(partner_setup):
    tid, _ = partner_setup
    with tenant_session(tid) as s:
        with pytest.raises(partners.PartnerError, match="already registered"):
            partners.register(s, name="Firm Outreach Desk", region="india", contact_email="desk@firmco.example",
                              actor="Sales Lead")
        with pytest.raises(partners.PartnerError, match="already registered"):
            partners.register(s, name="Sub", region="india", contact_email="a@mail.firmco.example", actor="x")


def test_M2_inactive_partner_blocked_even_after_queueing(partner_setup):
    tid, pid = partner_setup
    with tenant_session(tid) as s:
        m, _ = comms.draft_email(s, idempotency_key="k", to="ops@firmco.example", subject="Hello", body="Intro")
        comms.request_send(s, m.id)
        mid = m.id
        partners.deactivate(s, pid, actor="owner")  # deactivated while the message waits in the outbox
    comms.dispatch()
    with tenant_session(tid) as s:
        msg = s.get(OutboundMessage, mid)
        assert msg.status == "failed" and "inactive" in msg.last_error
    assert comms.OutboxTransport.delivered == []


def test_L2_partner_mail_cannot_carry_client_identity(partner_setup):
    tid, _ = partner_setup
    with tenant_session(tid) as s:
        with pytest.raises(ValueError, match="identifies a client"):
            comms.draft_email(s, idempotency_key="leak", to="ops@firmco.example", subject="Work",
                              body="Can you help with Zenith Holdings? Contact Priya Raman.")
        comms.draft_email(s, idempotency_key="ok", to="ops@firmco.example", subject="Intro",
                          body="We would like to discuss a referral partnership.")
