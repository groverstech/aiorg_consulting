"""Email delivery under failures, using the real SMTP transport against a local fake server."""
import socket
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from tests.fake_smtp import FakeSMTPServer
from wwos import comms
from wwos.api import app
from wwos.db import system_session, tenant_session
from wwos.models import OutboundMessage, utcnow


@pytest.fixture
def smtp_env(monkeypatch):
    def use(port, timeout="2"):
        monkeypatch.setenv("WWOS_EMAIL_TRANSPORT", "smtp")
        monkeypatch.setenv("WWOS_SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("WWOS_SMTP_PORT", str(port))
        monkeypatch.setenv("WWOS_SMTP_FROM", "ops@whitewaters.example")
        monkeypatch.setenv("WWOS_SMTP_SECURITY", "none")
        monkeypatch.setenv("WWOS_SMTP_TIMEOUT_SECONDS", timeout)
    return use


def queue(tid, n=1, body="Hello\n.\nA line that is just a dot above."):
    ids = []
    with tenant_session(tid) as s:
        for i in range(n):
            m, _ = comms.draft_email(s, idempotency_key=f"m{i}", to=f"client{i}@example.com",
                                     subject=f"Proposal {i}", body=body)
            comms.request_send(s, m.id)
            ids.append(m.id)
    return ids


def status(tid, mid):
    with tenant_session(tid) as s:
        m = s.get(OutboundMessage, mid)
        return m.status, m.attempts, m.last_error


def expire_backoff():
    with system_session() as s:
        s.execute(update(OutboundMessage).values(updated_at=utcnow() - timedelta(hours=1)))


def test_success_sets_stable_message_id_and_dot_stuffing(tenant, smtp_env):
    tid, _ = tenant
    with FakeSMTPServer("ok") as srv:
        smtp_env(srv.port)
        [mid] = queue(tid)
        assert comms.dispatch() == 1
        assert comms.dispatch() == 0
    assert status(tid, mid)[0] == "sent"
    assert len(srv.delivered) == 1
    raw = srv.delivered[0]
    assert f"Message-ID: <wwos.{mid}@whitewaters.example>".encode() in raw
    assert b"\r\n..\r\n" in raw  # a lone "." line is escaped on the wire


@pytest.mark.parametrize("mode,expected", [("rcpt_4xx", "queued"), ("data_cmd_4xx", "queued"),
                                           ("reject_after_data_4xx", "queued"), ("rcpt_5xx", "failed"),
                                           ("reject_after_data_5xx", "failed")])
def test_definite_rejections_retry_or_fail_without_delivering(tenant, smtp_env, mode, expected):
    tid, _ = tenant
    with FakeSMTPServer(mode) as srv:
        smtp_env(srv.port)
        [mid] = queue(tid)
        comms.dispatch()
        st, attempts, err = status(tid, mid)
        assert st == expected and attempts == 1 and "rejected" in err
        assert srv.delivered == []
        if expected == "queued":  # transient: once the server recovers, delivered exactly once
            srv.mode = "ok"
            comms.dispatch()
            assert status(tid, mid)[0] == "queued"  # still inside the backoff window
            expire_backoff()
            comms.dispatch()
            assert status(tid, mid)[0] == "sent" and len(srv.delivered) == 1


def test_unreachable_server_is_retryable(tenant, smtp_env):
    tid, _ = tenant
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here
    smtp_env(port)
    [mid] = queue(tid)
    comms.dispatch()
    assert status(tid, mid)[0] == "queued"


@pytest.mark.parametrize("mode", ["drop_after_data", "slow_after_data"])
def test_ambiguous_outcome_is_never_resent_automatically(tenant, smtp_env, mode):
    tid, key = tenant
    with FakeSMTPServer(mode, slow_seconds=3) as srv:
        smtp_env(srv.port, timeout="1")
        [mid] = queue(tid)
        comms.dispatch()
        st, _, err = status(tid, mid)
        assert st == "unknown" and "unknown" in err
        srv.mode = "ok"
        expire_backoff()
        for _ in range(3):
            comms.dispatch()
        assert srv.received_bodies == 1  # the server got it once; we never sent it again
        assert status(tid, mid)[0] == "unknown"
        # Owner reconciles: confirmed delivered in the mail server log.
        with TestClient(app) as c:
            r = c.post(f"/api/messages/{mid}/resolve", json={"delivered": True},
                       headers={"Authorization": f"Bearer {key}"})
            assert r.status_code == 200 and r.json()["status"] == "sent"
            again = c.post(f"/api/messages/{mid}/resolve", json={"delivered": True},
                           headers={"Authorization": f"Bearer {key}"})
            assert again.status_code == 409


def test_owner_can_requeue_unknown_message_once(tenant, smtp_env):
    tid, key = tenant
    with FakeSMTPServer("drop_after_data") as srv:
        smtp_env(srv.port)
        [mid] = queue(tid)
        comms.dispatch()
        assert status(tid, mid)[0] == "unknown"
        srv.mode = "ok"
        with tenant_session(tid) as s:
            comms.resolve(s, mid, delivered=False, actor="owner:test")
        comms.dispatch()
        assert status(tid, mid)[0] == "sent"
        ids = {raw.split(b"Message-ID: ")[1].split(b"\r\n")[0] for raw in srv.delivered}
        assert len(srv.delivered) == 2 and len(ids) == 1  # same Message-ID: recipients can de-duplicate


def test_concurrent_dispatchers_deliver_each_message_once(tenant, smtp_env):
    tid, _ = tenant
    with FakeSMTPServer("ok") as srv:
        smtp_env(srv.port)
        ids = queue(tid, n=20)
        barrier = threading.Barrier(4)

        def run():
            barrier.wait()
            comms.dispatch()

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(srv.delivered) == 20
    assert all(status(tid, m)[0] == "sent" for m in ids)


def test_worker_crash_mid_send_becomes_unknown_not_resent(tenant):
    tid, _ = tenant
    [mid] = queue(tid)
    with system_session() as s:  # simulate: claimed ("sending") then the worker died
        s.execute(update(OutboundMessage).values(status="sending", attempts=1,
                                                 updated_at=utcnow() - timedelta(minutes=11)))
    comms.dispatch()
    assert status(tid, mid)[0] == "unknown"
    assert comms.OutboxTransport.delivered == []


def test_resolve_requires_owner(tenant):
    from wwos.bootstrap import create_api_key

    tid, _ = tenant
    op = create_api_key(tid, "op", "operator")
    [mid] = queue(tid)
    with TestClient(app) as c:
        r = c.post(f"/api/messages/{mid}/resolve", json={"delivered": True},
                   headers={"Authorization": f"Bearer {op}"})
        assert r.status_code == 403


def test_tests_cannot_reach_the_internet():
    s = socket.socket()
    with pytest.raises(ConnectionRefusedError, match="non-local"):
        s.connect(("93.184.216.34", 25))
    s.close()
