"""Per-call budget enforcement for the Claude Agent SDK runtime.

The Agent SDK runs the Claude Code CLI as a subprocess and offers no hook before each model call
(its ``max_budget_usd`` is checked only after responses and can overshoot). The documented way to
control each sampling request is a proxy at ``ANTHROPIC_BASE_URL``. This module is that proxy,
running in-process on 127.0.0.1:

* Every ``POST <run-token>/v1/messages`` is admitted through ``budget_guard.reserve`` (the same
  atomic agent-lineage + organisation check the Messages runtime uses) BEFORE it is forwarded.
* ``max_tokens`` is clamped to the run's cap, so the reserved worst case is real.
* Actual usage is parsed from the response (including prompt-cache tokens, priced conservatively
  at the full input rate) and recorded on the reservation (``incur``).
* Only requests for the agent's configured model are forwarded.
* The real API key lives only here; the CLI subprocess holds a dummy key.

Each run registers a random token that encodes nothing; unknown tokens are refused.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .. import budget, budget_guard
from ..budget import BudgetExceeded


@dataclass
class RunBudget:
    tenant_id: str
    agent_id: str
    agent_name: str
    agent_budget: Decimal
    lineage_ids: list[str]
    task_id: str | None
    model: str
    max_output_tokens: int
    reservations: list[str] = field(default_factory=list)
    spend: list[tuple[str, int, int, str]] = field(default_factory=list)  # (model, in, out, reservation)
    refused: str | None = None
    refused_error: BudgetExceeded | None = None  # re-raised as-is (keeps PaceDeferred's retry time)
    upstream_status: int | None = None
    calls: int = 0
    count_calls: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


_runs: dict[str, RunBudget] = {}
_runs_lock = threading.Lock()
_server: ThreadingHTTPServer | None = None
_server_lock = threading.Lock()


def upstream_base() -> str:
    return os.environ.get("WWOS_ANTHROPIC_UPSTREAM", "https://api.anthropic.com").rstrip("/")


def register(run: RunBudget) -> str:
    token = secrets.token_urlsafe(24)
    with _runs_lock:
        _runs[token] = run
    return token


def unregister(token: str) -> RunBudget | None:
    with _runs_lock:
        return _runs.pop(token, None)


def base_url(token: str) -> str:
    return f"http://127.0.0.1:{ensure_started()}/run/{token}"


def ensure_started() -> int:
    global _server
    with _server_lock:
        if _server is None:
            _server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            _server.daemon_threads = True
            threading.Thread(target=_server.serve_forever, daemon=True, name="wwos-budget-proxy").start()
        return _server.server_address[1]


def _usage_from_sse(chunk_lines, usage: dict) -> None:
    for line in chunk_lines:
        if not line.startswith(b"data:"):
            continue
        try:
            ev = json.loads(line[5:].strip() or b"{}")
        except ValueError:
            continue
        u = None
        if ev.get("type") == "message_start":
            u = (ev.get("message") or {}).get("usage")
        elif ev.get("type") == "message_delta":
            u = ev.get("usage")
            usage["_final"] = True  # the response reached its end
        if isinstance(u, dict):
            for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                try:
                    if u.get(k) is not None:
                        usage[k] = max(usage.get(k, 0), int(u[k]))
                except (TypeError, ValueError):
                    usage["_unparseable"] = True


def _billable(usage: dict, cap: int, body_len: int) -> tuple[int, int]:
    """Conservative pricing in input-token units: cache writes at 2x (covers the most expensive cache
    tier), cache reads at 1x (actual 0.1x). Output defaults to the cap when the stream did not finish
    or usage could not be parsed, so a broken response is never under-billed."""
    if not usage or usage.get("_unparseable"):
        return body_len // 3 + 1, cap
    inp = (usage.get("input_tokens", 0) + 2 * usage.get("cache_creation_input_tokens", 0)
           + usage.get("cache_read_input_tokens", 0))
    out = usage.get("output_tokens", 0) if usage.get("_final") else cap
    return max(inp, 1), out


# Betas with premium pricing are stripped so the price table stays valid (e.g. long context, fast mode).
PREMIUM_BETA_MARKERS = ("context-1m", "fast-mode")
MAX_BODY_BYTES = 20 * 1024 * 1024
MAX_CONCURRENT = 32
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
MAX_COUNT_TOKENS_PER_RUN = 50


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep request logs out of application logs
        pass

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _error(self, status: int, message: str, kind: str = "invalid_request_error") -> None:
        self._reply(status, {"type": "error", "error": {"type": kind, "message": message}})

    def _run(self) -> tuple[RunBudget | None, str]:
        parts = self.path.split("/", 3)  # "", "run", token, rest
        if len(parts) < 4 or parts[1] != "run":
            return None, ""
        with _runs_lock:
            return _runs.get(parts[2]), "/" + parts[3]

    def do_GET(self):  # nothing but model calls is served
        self._error(404, "not available")

    def do_HEAD(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if not _slots.acquire(blocking=False):
            self._error(503, "proxy busy", "overloaded_error")
            return
        try:
            self._post()
        finally:
            _slots.release()

    def _post(self):
        run, rest = self._run()  # token checked BEFORE the body is read
        if run is None:
            self._error(403, "unknown run")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._error(413, "request body too large or invalid")
            return
        raw = self.rfile.read(length) if length else b""
        path = rest.split("?", 1)[0]
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            self._error(400, "invalid JSON")
            return
        if req.get("model") != run.model:
            self._error(400, f"model {req.get('model')!r} is not this agent's model")
            return
        if path == "/v1/messages/count_tokens":  # free, but still this agent's model and rate-limited
            with run.lock:
                run.count_calls += 1
                over = run.count_calls > MAX_COUNT_TOKENS_PER_RUN
            if over:
                self._error(429, "too many token-count calls in this run", "rate_limit_error")
                return
            self._forward(run, rest, json.dumps(req).encode(), rid=None)
            return
        if path != "/v1/messages":
            self._error(404, "not available")
            return
        req.pop("speed", None)  # fast mode is premium-priced
        cap = run.max_output_tokens
        if int(req.get("max_tokens") or cap) > cap:
            req["max_tokens"] = cap
            thinking = req.get("thinking") or {}
            if thinking.get("type") == "enabled" and int(thinking.get("budget_tokens", 0)) >= cap:
                thinking["budget_tokens"] = max(1024, cap - 1)
        body = json.dumps(req).encode()
        estimate = budget.worst_case_estimate(run.model, 2 * len(body), int(req["max_tokens"]))  # cache writes 2x
        try:
            rid = budget_guard.store().reserve(
                tenant_id=run.tenant_id, agent_id=run.agent_id, agent_name=run.agent_name,
                agent_budget=run.agent_budget, task_id=run.task_id, estimate=estimate, lineage_ids=run.lineage_ids)
        except BudgetExceeded as e:
            with run.lock:
                run.refused = str(e)
                run.refused_error = e
            self._error(400, f"WWOS budget refused this call: {e}")
            return
        with run.lock:
            run.reservations.append(rid)
            run.calls += 1
        self._forward(run, rest, body, rid=rid)

    def _forward(self, run: RunBudget, rest: str, body: bytes, *, rid: str | None) -> None:
        """Forward one request. Once the upstream has answered 200 the call is treated as billed:
        the reservation is never released, the upstream is drained even if the CLI disconnects, and
        anything not reported is priced at the worst case."""
        import httpx

        headers = {"content-type": "application/json", "x-api-key": os.environ.get("ANTHROPIC_API_KEY", "")}
        if self.headers.get("anthropic-version"):
            headers["anthropic-version"] = self.headers["anthropic-version"]
        betas = [b.strip() for b in (self.headers.get("anthropic-beta") or "").split(",") if b.strip()]
        betas = [b for b in betas if not any(m in b for m in PREMIUM_BETA_MARKERS)]
        if betas:
            headers["anthropic-beta"] = ",".join(betas)
        base = upstream_base()
        loopback = base.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))
        usage: dict = {}
        status: int | None = None
        client_gone = False
        try:
            # Real upstream: honour the deployment's egress proxy settings. Loopback (tests): never proxy.
            with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0), trust_env=not loopback) as client:
                with client.stream("POST", base + rest, content=body, headers=headers) as r:
                    status = run.upstream_status = r.status_code
                    try:
                        self.send_response(r.status_code)
                        self.send_header("Content-Type", r.headers.get("content-type", "application/json"))
                        self.send_header("Connection", "close")
                        self.end_headers()
                    except OSError:
                        client_gone = True
                    buf = b""
                    try:
                        for chunk in r.iter_bytes():
                            if not client_gone:
                                try:
                                    self.wfile.write(chunk)
                                except OSError:
                                    client_gone = True  # keep draining upstream to learn the usage
                            if rid and status == 200:
                                buf += chunk
                                *lines, buf = buf.split(b"\n")
                                _usage_from_sse(lines, usage)
                    except httpx.HTTPError:
                        pass  # upstream broke mid-stream: the call may still be billed (priced below)
                    if rid and status == 200:
                        _usage_from_sse([buf], usage)
                        if not usage and buf.strip().startswith(b"{"):
                            try:
                                msg = json.loads(buf)
                                _usage_from_sse([b"data: " + json.dumps({"type": "message_start", "message": msg}).encode()], usage)
                                usage["_final"] = True
                            except ValueError:
                                pass
            self.close_connection = True
        except Exception:  # noqa: BLE001 - connection to upstream failed
            if status is None:  # the request never reached the model: nothing billed
                if rid:
                    budget_guard.store().release(rid)
                run.upstream_status = 502
                try:
                    self._error(502, "upstream unavailable", "api_error")
                except OSError:
                    pass
                return
        if not rid:
            return
        if status != 200:
            budget_guard.store().release(rid)  # error responses are not billed
            return
        inp, out = _billable(usage, run.max_output_tokens, len(body))
        budget_guard.store().incur(rid, model=run.model, input_tokens=inp, output_tokens=out)
        with run.lock:
            run.spend.append((run.model, inp, out, rid))
