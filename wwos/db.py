"""Database engine, sessions and tenant isolation.

Tenant isolation is enforced centrally rather than by convention:

* Every tenant-owned model inherits ``TenantScoped``.
* A session opened with ``tenant_session(tenant_id)`` automatically adds
  ``tenant_id == :tenant`` to every ORM SELECT/UPDATE/DELETE touching a
  tenant-scoped entity (``with_loader_criteria``).
* A ``before_flush`` hook refuses to write any tenant-scoped row whose
  ``tenant_id`` differs from the session's tenant (and stamps it when unset).

A session without a tenant (``system_session``) is reserved for platform-level
operations such as provisioning tenants, migrations and backups.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import ForeignKey, String, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    ORMExecuteState,
    Session,
    declared_attr,
    mapped_column,
    sessionmaker,
    with_loader_criteria,
)

from .config import get_settings


class Base(DeclarativeBase):
    pass


class TenantScoped:
    """Mixin for every row that belongs to exactly one tenant."""

    @declared_attr
    def tenant_id(cls) -> Mapped[str]:  # noqa: N805
        return mapped_column(String(36), ForeignKey("tenants.id"), index=True, nullable=False)


class TenantIsolationError(PermissionError):
    """Raised when code attempts to read or write across tenant boundaries."""


_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _make_engine(url: str) -> Engine:
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
            # pysqlite defers BEGIN until the first DML, so a SAVEPOINT issued before any write runs
            # outside a transaction and its RELEASE commits. Take over transaction control so
            # savepoints nest properly and an outer rollback undoes everything (SQLAlchemy recipe).
            dbapi_conn.isolation_level = None
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.close()

        @event.listens_for(engine, "begin")
        def _sqlite_begin(conn):  # pragma: no cover - driver hook
            # Short write paths opt into BEGIN IMMEDIATE (write lock up front) so concurrent
            # read-then-write transactions queue on busy_timeout instead of deadlocking.
            if conn.get_execution_options().get("sqlite_immediate"):
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                conn.exec_driver_sql("BEGIN")

    return engine


def configure(url: str | None = None) -> Engine:
    """(Re)configure the global engine. Tests call this with a temp database."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = _make_engine(url or get_settings().database_url)
    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def engine() -> Engine:
    if _engine is None:
        configure()
    assert _engine is not None
    return _engine


@contextmanager
def write_transaction():
    """A short, independent read-then-write transaction outside any business session."""
    with engine().connect() as conn:
        conn = conn.execution_options(sqlite_immediate=True)
        with conn.begin():
            yield conn


def init_db() -> None:
    """Bring the schema to the latest revision via migrations (never ``create_all``), so the
    migration path is exercised everywhere. In production the app only *verifies* the schema;
    migrations run as a separate, backed-up deployment step (``wwos db upgrade``)."""
    from . import migrate, models  # noqa: F401  (register mappers)
    from .config import get_settings

    if get_settings().environment == "production":
        migrate.ensure_current()
    else:
        migrate.upgrade()


def _factory() -> sessionmaker[Session]:
    if _SessionFactory is None:
        configure()
    assert _SessionFactory is not None
    return _SessionFactory


@event.listens_for(Session, "do_orm_execute")
def _apply_tenant_filter(state: ORMExecuteState) -> None:
    tenant_id = state.session.info.get("tenant_id")
    if tenant_id is None:
        return
    if state.is_select or state.is_update or state.is_delete:
        state.statement = state.statement.options(
            with_loader_criteria(
                TenantScoped,
                lambda cls: cls.tenant_id == tenant_id,
                include_aliases=True,
            )
        )


@event.listens_for(Session, "before_flush")
def _guard_tenant_writes(session: Session, _ctx, _instances) -> None:
    tenant_id = session.info.get("tenant_id")
    if tenant_id is None:
        return
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        if isinstance(obj, TenantScoped):
            if getattr(obj, "tenant_id", None) is None and obj in session.new:
                obj.tenant_id = tenant_id
            if obj.tenant_id != tenant_id:
                raise TenantIsolationError(
                    f"{type(obj).__name__} belongs to another tenant; write refused"
                )


@contextmanager
def tenant_session(tenant_id: str, immediate: bool = False) -> Iterator[Session]:
    """``immediate=True`` (short read-then-write units such as outbox state changes) takes the SQLite
    write lock at BEGIN so concurrent writers queue instead of deadlocking. No effect on PostgreSQL."""
    if not tenant_id:
        raise TenantIsolationError("tenant_session requires a tenant id")
    from . import budget_guard

    session = _factory()()
    session.info["tenant_id"] = tenant_id
    if immediate:
        session.connection(execution_options={"sqlite_immediate": True})
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        # Model spend incurred inside the rolled-back transaction is written to the ledger anyway.
        budget_guard.after_rollback(session)
        raise
    finally:
        session.close()
    budget_guard.after_commit(session)


@contextmanager
def system_session(immediate: bool = False) -> Iterator[Session]:
    session = _factory()()
    if immediate:
        session.connection(execution_options={"sqlite_immediate": True})
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
