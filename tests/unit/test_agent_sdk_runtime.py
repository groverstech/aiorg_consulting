"""Claude Agent SDK runtime: offline tests against a local fake Anthropic API.

The real Claude Code CLI (bundled with claude-agent-sdk) runs as a subprocess; only the model is fake.
"""
import json
import os
from decimal import Decimal

import pytest
from sqlalchemy import func, select

pytest.importorskip("claude_agent_sdk")

from tests.fake_anthropic import FakeAnthropic, last_has_tool_result  # noqa: E402
from wwos.agents.runtime import find_agent, run_agent_prompt  # noqa: E402
from wwos.budget import BudgetExceeded  # noqa: E402
from wwos.db import tenant_session  # noqa: E402
from wwos.models import ApprovalRequest, Contact, CostEntry, Tenant  # noqa: E402

TOOL = "mcp__wwos__"


@pytest.fixture
def sdk(monkeypatch):
    def use(responder, usage=(1000, 200)):
        fake = FakeAnthropic(responder, usage)
        monkeypatch.setenv("WWOS_AGENT_RUNTIME", "agent_sdk")
        monkeypatch.setenv("WWOS_ANTHROPIC_UPSTREAM", fake.url)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-key-held-by-proxy")
        return fake
    return use


def script(*steps):
    """Tool steps are issued only while the conversation is fresh or answering tool results."""
    queue = list(steps)

    def responder(body):
        return queue.pop(0) if queue else ("text", "done")
    return responder


def run(tid, role, prompt="go", **kw):
    with tenant_session(tid) as s:
        return run_agent_prompt(s, s.get(Tenant, tid), find_agent(s, role=role), prompt, **kw)


def ledger(tid):
    with tenant_session(tid) as s:
        return Decimal(str(s.execute(select(func.coalesce(func.sum(CostEntry.cost_usd), 0))).scalar_one()))


def test_gated_tool_runs_and_every_call_is_reserved_and_recorded(tenant, sdk):
    tid, _ = tenant
    with sdk(script(("tool", TOOL + "crm_upsert_contact", {"email": "a@b.com", "first_name": "A"}),
                    ("text", "Contact created."))) as fake:
        text = run(tid, "sales", max_tokens=2000)
    assert text == "Contact created."
    with tenant_session(tid) as s:
        assert s.scalars(select(Contact)).one().email == "a@b.com"
    calls = fake.model_requests()
    assert len(calls) == 2
    # Only this agent's gated tools exist in the session: no shell, file, web or sub-agent tools.
    names = {t["name"] for t in calls[0]["body"]["tools"]}
    assert all(n.startswith(TOOL) or n == "DeferredToolPlaceholder" for n in names)
    assert TOOL + "crm_upsert_contact" in names and TOOL + "finance_transfer_funds" not in names
    # max_tokens clamped to the run cap; the real key is added by the proxy, never by the CLI.
    assert all(c["body"]["max_tokens"] <= 2000 for c in calls)
    assert all(c["headers"].get("x-api-key") == "sk-real-key-held-by-proxy" for c in calls)
    # 2 calls x (1000 in, 200 out) on Opus 5.5 = 2 x ($0.004 + $0.004)
    assert ledger(tid) == Decimal("0.016")


def test_tools_not_held_and_builtins_never_execute(tenant, sdk):
    tid, _ = tenant
    with sdk(script(("tool", "Bash", {"command": "cat /etc/passwd"}),
                    ("tool", TOOL + "finance_transfer_funds", {"amount": "5", "currency": "USD", "payee": "x"}),
                    ("tool", "mcp__evil__exfiltrate", {}), ("text", "ok"))):
        run(tid, "marketing")
    with tenant_session(tid) as s:
        assert s.scalars(select(ApprovalRequest)).all() == []  # nothing reached any gate or handler


def test_owner_approval_gate_holds_under_the_sdk(tenant, sdk):
    tid, _ = tenant
    with sdk(script(("tool", TOOL + "finance_transfer_funds", {"amount": "5000", "currency": "USD", "payee": "X"}),
                    ("text", "Requested."))):
        run(tid, "finance")
    with tenant_session(tid) as s:
        req = s.scalars(select(ApprovalRequest)).one()
        assert req.category == "financial_transfer" and req.status == "pending"


def test_budget_refused_before_the_model_is_called(tenant, sdk):
    tid, _ = tenant
    with tenant_session(tid) as s:
        find_agent(s, role="qa").monthly_budget_usd = Decimal("0.01")
    with sdk(script(("text", "never"))) as fake:
        with pytest.raises(BudgetExceeded):
            run(tid, "qa", max_tokens=4096)  # worst case > $0.01
    assert fake.model_requests() == []
    assert ledger(tid) == Decimal("0")


def test_schema_validation_still_applies_to_sdk_tool_calls(tenant, sdk):
    tid, _ = tenant
    with sdk(script(("tool", TOOL + "crm_upsert_contact", {"email": "a@b.com", "first_name": "A",
                                                           "can_provision": True}), ("text", "x"))):
        try:
            run(tid, "sales")
        except Exception:  # the CLI may reject the extra key itself; either way nothing is written
            pass
    with tenant_session(tid) as s:
        assert s.scalars(select(Contact)).all() == []


def test_no_temporary_state_left_behind(tenant, sdk, tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    tid, _ = tenant
    with sdk(script(("text", "hi"))):
        run(tid, "operations")
    assert [p for p in os.listdir(tmp_path) if p.startswith("wwos-agent")] == []


def test_subprocess_environment_carries_no_secrets(monkeypatch):
    from wwos.agents.sdk_runtime import subprocess_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
    monkeypatch.setenv("WWOS_DB_PASSWORD", "dbpw")
    monkeypatch.setenv("WWOS_SMTP_PASSWORD", "smtp")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
    env = subprocess_env("http://127.0.0.1:1/run/x", "/tmp/c", 4096)
    assert env["ANTHROPIC_API_KEY"] != "sk-real" and env["WWOS_DB_PASSWORD"] == "" and env["WWOS_SMTP_PASSWORD"] == ""
    assert env["HTTPS_PROXY"] == "" and env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def test_unknown_run_tokens_and_other_paths_are_refused():
    import httpx

    from wwos.agents import budget_proxy

    port = budget_proxy.ensure_started()
    r = httpx.post(f"http://127.0.0.1:{port}/run/forged/v1/messages", json={"model": "claude-opus-5-5"})
    assert r.status_code == 403
    assert httpx.get(f"http://127.0.0.1:{port}/run/x/v1/models").status_code == 404


def test_ceo_provisions_a_specialist_through_the_sdk(tenant, sdk):
    """Dynamic agent creation (including the new agent's acceptance test) runs entirely on the SDK."""
    from wwos.models import Agent

    tid, _ = tenant
    spec = {"name": "Research Analyst", "role": "specialist", "department_code": "sales",
            "instructions": "You research prospective CA and CS partner firms and summarise findings for the CEO.",
            "tools": ["partners.list"], "monthly_budget_usd": 5}

    def responder(body):
        system = json.dumps(body.get("system"))
        if "Research Analyst" in system:
            return ("text", "I am the Research Analyst.")
        return ("text", "Provisioned.") if last_has_tool_result(body) else ("tool", TOOL + "agents_provision", spec)

    with sdk(responder):
        run(tid, "ceo")
    with tenant_session(tid) as s:
        a = s.scalars(select(Agent).where(Agent.name == "Research Analyst")).one()
        assert a.status == "active" and a.created_by == "CEO" and a.tools == ["partners.list"]


def test_full_autonomous_organisation_scenario_on_the_agent_sdk(tenant, sdk):
    """The complete CEO objective scenario (plan, provision, delegate, QA reject/retry, outage ->
    reassignment, CRM, partner brief, executive report, QA scan) with every agent on the Agent SDK."""
    from tests.acceptance.test_autonomous_organisation import run_ceo_scenario
    from tests.fake_anthropic import provider_responder

    holder = {}

    def responder(body):
        return provider_responder(holder["sim"])(body) if "sim" in holder else ("text", "ok")

    with sdk(responder) as fake:
        run_ceo_scenario(tenant, lambda sim: holder.update(sim=sim))
    assert len(fake.model_requests()) > 20  # every agent turn went through the CLI and the budget proxy


def test_inherited_environment_never_reaches_the_model(tenant, sdk, monkeypatch):
    """The CLI injects some inherited CLAUDE_* variables (e.g. a user e-mail) into prompts. The launcher's
    environment allowlist must keep every such value away from the model."""
    tid, _ = tenant
    canary = "leak-canary-7f3a@example.invalid"
    for name in ("CLAUDE_CODE_USER_EMAIL", "CLAUDE_CODE_ORGANIZATION_UUID", "WWOS_DB_PASSWORD", "SOME_APP_SETTING"):
        monkeypatch.setenv(name, canary)
    with sdk(script(("text", "ok"))) as fake:
        run(tid, "operations")
    blob = json.dumps(fake.requests)
    assert fake.model_requests() and canary not in blob


def test_cli_does_not_outlive_a_killed_worker(tenant, tmp_path):
    """If the worker process dies (SIGKILL) mid-run, its CLI subprocess must die with it."""
    import signal
    import subprocess
    import sys
    import time
    import uuid

    from wwos import db

    tid, _ = tenant
    marker = f"orphan-check-{uuid.uuid4().hex[:8]}"
    script = tmp_path / "worker.py"
    script.write_text(f"""
import os, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class Slow(BaseHTTPRequestHandler):
    def do_POST(self):
        time.sleep(120)
srv = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ.update(WWOS_AGENT_RUNTIME="agent_sdk", WWOS_ANTHROPIC_UPSTREAM=f"http://127.0.0.1:{{srv.server_address[1]}}",
                  WWOS_DATABASE_URL={str(db.engine().url)!r}, ANTHROPIC_API_KEY="sk-test")
from wwos import db
db.configure(os.environ["WWOS_DATABASE_URL"])
from wwos.agents.runtime import run_agent_prompt, find_agent
from wwos.models import Tenant
with db.tenant_session({tid!r}) as s:
    run_agent_prompt(s, s.get(Tenant, {tid!r}), find_agent(s, role="operations"), {marker!r})
""")
    proc = subprocess.Popen([sys.executable, str(script)], cwd=os.getcwd())
    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            children = subprocess.run(["pgrep", "-P", str(proc.pid)], capture_output=True, text=True).stdout.split()
            if children:
                break
            time.sleep(0.5)
        assert children, "worker never started the CLI"
        time.sleep(2)
        proc.send_signal(signal.SIGKILL)
        proc.wait(10)
        time.sleep(2)
        alive = [c for c in children if os.path.exists(f"/proc/{c}")]
        assert alive == [], f"CLI processes outlived the worker: {alive}"
    finally:
        if proc.poll() is None:
            proc.kill()
