"""A tiny, local-only SMTP server whose failure modes can be chosen per test.

Behaviours: ok | rcpt_4xx | rcpt_5xx | data_cmd_4xx | reject_after_data_4xx | reject_after_data_5xx |
drop_after_data (body received, connection closed without a reply) | slow_after_data (reply after the
client's timeout). Accepted messages are recorded in ``delivered`` (raw bytes).
"""
from __future__ import annotations

import socket
import socketserver
import threading
import time


class _Handler(socketserver.StreamRequestHandler):
    def _send(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())
        self.wfile.flush()

    def handle(self) -> None:
        srv: FakeSMTPServer = self.server.owner  # type: ignore[attr-defined]
        mode = srv.mode
        self._send("220 fake.local ESMTP")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            cmd = line.decode(errors="replace").strip().upper()
            if cmd.startswith(("EHLO", "HELO")):
                self._send("250 fake.local")
            elif cmd.startswith("MAIL FROM"):
                self._send("250 OK")
            elif cmd.startswith("RCPT TO"):
                if mode == "rcpt_4xx":
                    self._send("450 mailbox busy")
                elif mode == "rcpt_5xx":
                    self._send("550 no such user")
                else:
                    self._send("250 OK")
            elif cmd == "DATA":
                if mode == "data_cmd_4xx":
                    self._send("451 try later")
                    continue
                self._send("354 end with .")
                body = b""
                while True:
                    chunk = self.rfile.readline()
                    if not chunk or chunk == b".\r\n":
                        break
                    body += chunk
                srv.received_bodies += 1
                if mode == "drop_after_data":
                    srv.delivered.append(body)  # the server *did* take it, but the client never learns
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if mode == "slow_after_data":
                    srv.delivered.append(body)
                    time.sleep(srv.slow_seconds)
                    return
                if mode == "reject_after_data_4xx":
                    self._send("451 local error")
                elif mode == "reject_after_data_5xx":
                    self._send("554 rejected as spam")
                else:
                    srv.delivered.append(body)
                    self._send("250 queued")
            elif cmd == "QUIT":
                self._send("221 bye")
                return
            elif cmd in ("RSET", "NOOP"):
                self._send("250 OK")
            else:
                self._send("502 not implemented")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class FakeSMTPServer:
    def __init__(self, mode: str = "ok", slow_seconds: float = 2.0) -> None:
        self.mode = mode
        self.slow_seconds = slow_seconds
        self.delivered: list[bytes] = []
        self.received_bodies = 0
        self._srv = _Server(("127.0.0.1", 0), _Handler)
        self._srv.owner = self  # type: ignore[attr-defined]
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def __enter__(self) -> "FakeSMTPServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()
