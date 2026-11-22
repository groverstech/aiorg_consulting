"""Backup and restore.

SQLite: online, consistent copy via the sqlite3 backup API, then verified with
``PRAGMA integrity_check`` and a row-count comparison.
PostgreSQL: use managed snapshots or ``pg_dump``/``pg_restore`` (docs/RUNBOOK.md);
``verify`` works against any database URL.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, text

from .config import get_settings


def _sqlite_path(url: str) -> str:
    if not url.startswith("sqlite:///"):
        raise ValueError("file-level backup supports SQLite; use pg_dump for PostgreSQL (see RUNBOOK)")
    return url[len("sqlite:///"):]


def table_counts(url: str) -> dict[str, int]:
    eng = create_engine(url)
    try:
        with eng.connect() as c:
            return {t: c.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar_one()
                    for t in inspect(eng).get_table_names()}
    finally:
        eng.dispose()


def backup(dest_dir: str = "backups", url: str | None = None) -> str:
    url = url or get_settings().database_url
    src_path = _sqlite_path(url)
    os.makedirs(dest_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = os.path.join(dest_dir, f"wwos-{stamp}.db")
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    verify(dest, expected=table_counts(url))
    return dest


def verify(path: str, expected: dict[str, int] | None = None) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        raise RuntimeError(f"backup integrity check failed: {result}")
    counts = table_counts(f"sqlite:///{path}")
    if expected is not None and counts != expected:
        raise RuntimeError(f"backup row counts differ from source: {counts} vs {expected}")
    return counts


def restore(backup_path: str, url: str | None = None) -> dict[str, int]:
    """Restore a verified backup over the target database (owner-only, irreversible)."""
    url = url or get_settings().database_url
    counts = verify(backup_path)
    target = _sqlite_path(url)
    src = sqlite3.connect(backup_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    restored = table_counts(url)
    if restored != counts:
        raise RuntimeError("restored database does not match backup")
    return restored
