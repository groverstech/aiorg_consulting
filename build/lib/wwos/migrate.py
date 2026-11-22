"""Database schema migrations (Alembic) with production safeguards.

Commands (see ``wwos db --help``):

* ``status``   - current revision, head revision, pending revisions, drift.
* ``upgrade``  - apply pending revisions. SQLite: automatic verified backup first.
                 PostgreSQL: requires ``--backup-confirmed`` (take a Cloud SQL backup /
                 pg_dump first). Revisions marked ``destructive = True`` additionally
                 require ``--allow-destructive``.
* ``downgrade``- roll back to a revision; always destructive-gated.
* ``check``    - exit non-zero if the live schema differs from the models (drift).

Databases created before migrations existed (``create_all``) are *adopted*: their
schema is verified against the baseline revision and then stamped, never altered blindly.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine

from . import db

MIGRATIONS = Path(__file__).parent / "migrations"
BASELINE = "0001"


class MigrationError(RuntimeError):
    pass


def _config(connection=None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def script() -> ScriptDirectory:
    return ScriptDirectory.from_config(_config())


def head() -> str:
    return script().get_current_head()


def current(eng: Engine | None = None) -> str | None:
    eng = eng or db.engine()
    with eng.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _app_tables(eng: Engine) -> set[str]:
    return {t for t in inspect(eng).get_table_names() if t != "alembic_version"}


def pending(eng: Engine | None = None) -> list[str]:
    """Revisions not yet applied, oldest first."""
    return _path(current(eng or db.engine()), "head")


def _is_destructive(rev_id: str) -> bool:
    rev = script().get_revision(rev_id)
    spec = importlib.util.spec_from_file_location(f"_wwos_rev_{rev_id}", rev.path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return bool(getattr(mod, "destructive", False))


def drift(eng: Engine | None = None) -> list:
    """Differences between the live schema and the ORM models (empty list = in sync)."""
    eng = eng or db.engine()
    with eng.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return compare_metadata(ctx, db.Base.metadata)


def _baseline_signature() -> dict[str, set[str]]:
    """Table -> column names of the baseline revision, built in a scratch SQLite database."""
    scratch = create_engine("sqlite://")
    with scratch.connect() as conn:
        command.upgrade(_config(conn), BASELINE)
        conn.commit()
    insp = inspect(scratch)
    sig = {t: {c["name"] for c in insp.get_columns(t)} for t in insp.get_table_names() if t != "alembic_version"}
    scratch.dispose()
    return sig


def adopt(eng: Engine | None = None) -> str:
    """Stamp a pre-migrations database at the baseline after verifying its schema matches."""
    eng = eng or db.engine()
    if current(eng) is not None:
        raise MigrationError("database is already under migration control")
    insp = inspect(eng)
    live = {t: {c["name"] for c in insp.get_columns(t)} for t in _app_tables(eng)}
    expected = _baseline_signature()
    if live != expected:
        missing = {t: sorted(expected[t] - live.get(t, set())) for t in expected if expected[t] - live.get(t, set())}
        extra = {t: sorted(live[t] - expected.get(t, set())) for t in live if live[t] - expected.get(t, set())}
        raise MigrationError(f"schema does not match the baseline; refusing to adopt. missing={missing} extra={extra}")
    with eng.connect() as conn:
        command.stamp(_config(conn), BASELINE)
        conn.commit()
    return BASELINE


@dataclass
class UpgradeResult:
    before: str | None
    after: str
    applied: list[str]
    backup: str | None


def upgrade(target: str = "head", *, backup_confirmed: bool = False, allow_destructive: bool = False,
            backup_dir: str = "backups", eng: Engine | None = None) -> UpgradeResult:
    eng = eng or db.engine()
    url = str(eng.url)
    before = current(eng)
    if before is None and _app_tables(eng):
        adopt(eng)
        before = BASELINE
    todo = _path(before, target)
    if not todo:
        return UpgradeResult(before, before or head(), [], None)
    destructive = [r for r in todo if _is_destructive(r)]
    if destructive and not allow_destructive:
        raise MigrationError(f"revisions {destructive} can lose data; re-run with --allow-destructive "
                             "after the owner has authorised it")
    backup_path = None
    has_data = bool(_app_tables(eng))
    if has_data:
        if eng.dialect.name == "sqlite" and url not in ("sqlite://", "sqlite:///:memory:"):
            from .backup import backup as sqlite_backup

            backup_path = sqlite_backup(backup_dir, url=url.replace("sqlite:///", "sqlite:///", 1))
        elif eng.dialect.name != "sqlite" and not backup_confirmed:
            raise MigrationError("take a database backup first (Cloud SQL on-demand backup or pg_dump), "
                                 "then re-run with --backup-confirmed")
    with eng.connect() as conn:
        command.upgrade(_config(conn), target)
        conn.commit()
    return UpgradeResult(before, current(eng), todo, backup_path)


def _path(start: str | None, target: str) -> list[str]:
    """Revision ids strictly after ``start`` up to and including ``target``, oldest first."""
    if target == "head":
        target = head()
    if start == target:
        return []
    revs = [rev.revision for rev in script().iterate_revisions(target, start or "base")]
    return [r for r in reversed(revs) if r != start]


def downgrade(target: str, *, allow_destructive: bool, backup_confirmed: bool, eng: Engine | None = None) -> str:
    """Roll back schema. Always treated as destructive (dropped columns/tables lose data)."""
    eng = eng or db.engine()
    if not (allow_destructive and backup_confirmed):
        raise MigrationError("downgrade can lose data: requires --allow-destructive and --backup-confirmed")
    with eng.connect() as conn:
        command.downgrade(_config(conn), target)
        conn.commit()
    return current(eng)


def ensure_current(eng: Engine | None = None) -> None:
    """Startup check for production: refuse to run against an unmigrated or drifted schema."""
    eng = eng or db.engine()
    cur = current(eng)
    if cur != head():
        raise MigrationError(f"database schema is at {cur}, expected {head()}; run `wwos db upgrade` first")
