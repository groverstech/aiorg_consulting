from __future__ import annotations

import ipaddress
import socket

import pytest

from wwos import comms, db, llm
from wwos.llm import LLMResult


_real_connect = socket.socket.connect


def _guarded_connect(sock, address):
    """No test may reach the outside world: only loopback and local (unix) sockets are allowed.
    This makes accidental real e-mail or paid AI calls impossible from the test suite."""
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        host = address[0]
        try:
            ok = ipaddress.ip_address(host).is_loopback
        except ValueError:
            ok = host == "localhost"
        if not ok:
            raise ConnectionRefusedError(f"tests may not connect to non-local address {host!r}")
    return _real_connect(sock, address)


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setenv("WWOS_EMAIL_TRANSPORT", "outbox")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    monkeypatch.setenv("WWOS_DATABASE_URL", url)
    monkeypatch.setenv("WWOS_LLM_PROVIDER", "offline")
    db.configure(url)
    db.init_db()
    llm.set_provider(None)
    comms.set_transport(None)
    comms.OutboxTransport.delivered.clear()
    from wwos import api

    api._login_attempts.clear()
    yield url
    llm.set_provider(None)
    comms.set_transport(None)


@pytest.fixture
def tenant():
    from wwos.bootstrap import create_tenant

    return create_tenant("White Waters Consulting Group", "500")


@pytest.fixture
def other_tenant():
    from wwos.bootstrap import create_tenant

    return create_tenant("Other Co", "200")


class ScriptedProvider:
    """Returns queued results; bills as a real model so budget logic is exercised."""

    name = "scripted"

    def __init__(self, *, model="claude-opus-5-5", input_tokens=1000, output_tokens=1000, script=None,
                 error=None):
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.script = list(script or [])
        self.error = error
        self.calls = 0

    def complete(self, *, model, system, messages, tools, max_tokens):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.script:
            blocks = self.script.pop(0)
        else:
            blocks = [{"type": "text", "text": "done"}]
        calls = [b for b in blocks if b["type"] == "tool_use"]
        return LLMResult(content=blocks, stop_reason="tool_use" if calls else "end_turn",
                         input_tokens=self.input_tokens, output_tokens=self.output_tokens,
                         model=self.model, tool_calls=calls)


@pytest.fixture
def scripted():
    def make(**kw):
        p = ScriptedProvider(**kw)
        llm.set_provider(p)
        return p
    return make
