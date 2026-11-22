"""Local fake of the Anthropic Messages API (streaming SSE + JSON), for offline Agent SDK tests.

``responder(request_json) -> step`` decides each reply: ("tool", name, input), ("tools", [(name, input), ...])
or ("text", str).
Every request (path, headers, body) is recorded in ``requests``.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _sse(ev: str, data: dict) -> bytes:
    return f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()


class FakeAnthropic:
    def __init__(self, responder, usage=(1000, 200)) -> None:
        self.responder = responder
        self.usage = usage
        self.requests: list[dict] = []
        owner = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                owner.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if "count_tokens" in self.path:
                    return self._json(200, {"input_tokens": 10})
                step = owner.responder(body) or ("text", "done")
                if step[0] == "error":
                    return self._json(step[1], {"type": "error", "error": {"type": "invalid_request_error",
                                                                          "message": step[2]}})
                if step[0] == "tool":
                    step = ("tools", [(step[1], step[2])])
                stop = "tool_use" if step[0] == "tools" else "end_turn"
                n = len(owner.requests)
                blocks = ([{"type": "tool_use", "id": f"toolu_{n}_{i}", "name": nm, "input": inp}
                           for i, (nm, inp) in enumerate(step[1])] if step[0] == "tools"
                          else [{"type": "text", "text": step[1]}])
                msg = {"id": f"msg_{n}", "type": "message", "role": "assistant",
                       "model": body.get("model"), "content": [], "stop_reason": None, "stop_sequence": None,
                       "usage": {"input_tokens": owner.usage[0], "output_tokens": 0}}
                if not body.get("stream"):
                    full = dict(msg, content=blocks, stop_reason=stop,
                                usage={"input_tokens": owner.usage[0], "output_tokens": owner.usage[1]})
                    return self._json(200, full)
                out = _sse("message_start", {"type": "message_start", "message": msg})
                for i, b in enumerate(blocks):
                    start = dict(b, input={}) if b["type"] == "tool_use" else dict(b, text="")
                    out += _sse("content_block_start", {"type": "content_block_start", "index": i, "content_block": start})
                    delta = ({"type": "input_json_delta", "partial_json": json.dumps(b["input"])} if b["type"] == "tool_use"
                             else {"type": "text_delta", "text": b["text"]})
                    out += _sse("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta})
                    out += _sse("content_block_stop", {"type": "content_block_stop", "index": i})
                out += _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop,
                                              "stop_sequence": None}, "usage": {"output_tokens": owner.usage[1]}})
                out += _sse("message_stop", {"type": "message_stop"})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def _json(self, status, payload):
                b = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._srv.shutdown()
        self._srv.server_close()

    def model_requests(self):
        return [r for r in self.requests if r["path"].split("?")[0].endswith("/v1/messages")]


def last_has_tool_result(body: dict) -> bool:
    last = body["messages"][-1]
    return isinstance(last.get("content"), list) and any(b.get("type") == "tool_result" for b in last["content"])


def agent_name(body: dict) -> str:
    system = body.get("system")
    text = system if isinstance(system, str) else " ".join(b.get("text", "") for b in system or [])
    import re

    found = re.findall(r"You are ([^.,]+?), the ", text)
    return found[-1] if found else "?"


def provider_responder(provider, tool_prefix="mcp__wwos__"):
    """Adapt a provider-level test double (``complete(model, system, messages, tools, max_tokens)``)
    to the HTTP API, translating SDK tool names <-> WWOS tool names, so the same simulated model can
    drive either runtime."""
    import re

    from wwos.tools import from_api_name

    def to_sdk(api_tool_name: str) -> str:
        return tool_prefix + re.sub(r"[^a-zA-Z0-9_]", "_", from_api_name(api_tool_name))

    def text_of(content):
        if isinstance(content, str):
            return content
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")

    def responder(body):
        system = body.get("system")
        system = system if isinstance(system, str) else " ".join(b.get("text", "") for b in system or [])
        messages = []
        for m in body["messages"]:
            c = m["content"]
            if isinstance(c, list) and any(b.get("type") == "tool_result" for b in c):
                c = [{"type": "tool_result", "tool_use_id": b.get("tool_use_id"), "content": text_of(b.get("content", ""))}
                     for b in c if b.get("type") == "tool_result"]
            elif m["role"] == "user":
                c = text_of(c)
            messages.append({"role": m["role"], "content": c})
        try:
            result = provider.complete(model=body.get("model"), system=system, messages=messages,
                                       tools=body.get("tools", []), max_tokens=body.get("max_tokens"))
        except RuntimeError as e:  # simulated model outage -> a non-retryable API error
            return ("error", 400, str(e))
        if result.tool_calls:
            return ("tools", [(to_sdk(c["name"]), c["input"]) for c in result.tool_calls])
        return ("text", result.text or "done")
    return responder
