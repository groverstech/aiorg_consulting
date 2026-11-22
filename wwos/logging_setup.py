"""Logging: human-readable locally, structured JSON (Cloud Logging severities) in production."""
from __future__ import annotations

import json
import logging
import sys

from .config import get_settings


class JsonFormatter(logging.Formatter):
    LEVELS = {"WARNING": "WARNING", "ERROR": "ERROR", "CRITICAL": "CRITICAL", "INFO": "INFO", "DEBUG": "DEBUG"}

    def format(self, record: logging.LogRecord) -> str:
        entry = {"severity": self.LEVELS.get(record.levelname, "DEFAULT"), "message": record.getMessage(),
                 "logger": record.name}
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def configure() -> None:
    handler = logging.StreamHandler(sys.stdout)
    if get_settings().environment == "production":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
