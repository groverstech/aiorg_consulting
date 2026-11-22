"""Real-browser tests of the dashboard (headless Chromium via Playwright, local server only)."""
import os
import socket
import threading
import time

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.browser

LEAD = {"email": "jane@acme.com", "first_name": "Jane", "company": "Acme Ltd",
        "need": "We need help redesigning our operations model for growth", "estimated_value": "25000"}


def _chromium_path():
    for p in (os.environ.get("WWOS_CHROMIUM"), "/opt/pw-browsers/chromium"):
        if p and os.path.exists(p):
            return p
    return None


@pytest.fixture
def server(tenant):
    import uvicorn

    from wwos import workflows
    from wwos.api import app
    from wwos.db import tenant_session

    tid, key = tenant
    with tenant_session(tid) as s:
        workflows.start_lead_workflow(s, LEAD)
    workflows.drain()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", key
    srv.should_exit = True
    t.join(5)


@pytest.fixture
def browser():
    with playwright_sync.sync_playwright() as p:
        kwargs = {"executable_path": _chromium_path()} if _chromium_path() else {}
        try:
            b = p.chromium.launch(**kwargs)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {e}")
        yield b
        b.close()


def _login(page, base, key):
    page.goto(f"{base}/login")
    page.fill("input[name=api_key]", key)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base}/dashboard")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_dashboard_renders_without_errors(server, browser, scheme):
    base, key = server
    page = browser.new_page(color_scheme=scheme, viewport={"width": 1280, "height": 900})
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    _login(page, base, key)
    for heading in ["Owner approvals", "CEO objectives", "Pipeline", "Agent workforce", "Recent tasks"]:
        assert page.get_by_role("heading", name=heading).is_visible()
    assert page.get_by_text("jane@acme.com").is_visible()
    assert errors == []


def test_owner_approves_email_in_the_browser(server, browser):
    from wwos import comms, workflows

    base, key = server
    page = browser.new_page()
    _login(page, base, key)
    page.get_by_text("Read email").click()
    assert page.get_by_text("Dear Jane").is_visible()
    page.get_by_role("button", name="Approve").click()
    page.wait_for_url(f"{base}/dashboard")
    assert page.get_by_text("Nothing needs your decision.").is_visible()
    workflows.drain()
    assert len(comms.OutboxTransport.delivered) == 1


def test_mobile_layout_has_no_horizontal_scroll(server, browser):
    base, key = server
    page = browser.new_page(viewport={"width": 390, "height": 844})
    _login(page, base, key)
    assert page.evaluate("document.documentElement.scrollWidth") <= 390


def test_cross_site_write_with_session_cookie_is_refused(server, browser):
    base, key = server
    page = browser.new_page()
    _login(page, base, key)
    r = page.request.post(f"{base}/api/contacts", data={"email": "x@y.com", "first_name": "X"},
                          headers={"Origin": "https://evil.example"})
    assert r.status == 403
