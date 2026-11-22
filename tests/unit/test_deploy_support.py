import json
import logging
import urllib.request

from wwos import workflows
from wwos.config import get_settings
from wwos.logging_setup import JsonFormatter


def test_database_url_built_from_cloud_sql_socket(monkeypatch):
    monkeypatch.delenv("WWOS_DATABASE_URL")
    monkeypatch.setenv("WWOS_DATABASE_HOST_SOCKET", "/cloudsql/proj:us-central1:wwos-pg")
    monkeypatch.setenv("WWOS_DB_PASSWORD", "p@ss/word")
    url = get_settings().database_url
    assert url == "postgresql+psycopg://wwos:p%40ss%2Fword@/wwos?host=%2Fcloudsql%2Fproj%3Aus-central1%3Awwos-pg"


def test_structured_logs_carry_severity():
    rec = logging.LogRecord("wwos.worker", logging.ERROR, __file__, 1, "task %s -> %s", ("t1", "dead"), None)
    entry = json.loads(JsonFormatter().format(rec))
    assert entry == {"severity": "ERROR", "message": "task t1 -> dead", "logger": "wwos.worker"}


def test_worker_health_endpoint():
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    workflows.start_health_server(port)
    assert urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz").status == 200
