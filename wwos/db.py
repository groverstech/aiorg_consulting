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
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

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


def init_db() -> None:
    from . import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(engine())


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
def tenant_session(tenant_id: str) -> Iterator[Session]:
    if not tenant_id:
        raise TenantIsolationError("tenant_session requires a tenant id")
    session = _factory()()
    session.info["tenant_id"] = tenant_id
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def system_session() -> Iterator[Session]:
    session = _factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
