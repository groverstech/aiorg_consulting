"""Atomic budget reservations.

Every model call is preceded by ``reserve`` and followed by ``incur`` (or ``release``).
The check that admits a call is:

    committed ledger spend this month
  + every *active* reservation (reserved estimates and incurred-but-not-yet-committed actuals)
  + this call's worst-case estimate
  <= limit            (checked for the agent and for the organisation)

and it is made atomically with recording the new reservation, so concurrent workers can
never jointly exceed a limit.

Stores:

* ``SqlReservationStore`` (PostgreSQL, production): reservations live in their own committed
  transactions on a separate connection, serialised per tenant by ``SELECT ... FOR UPDATE``
  on ``budget_locks``. Visible to every worker on every host.
* ``MemoryReservationStore`` (SQLite, development/tests): process-local, serialised by a lock.
  SQLite deployments are single-process by design (production refuses SQLite).

Ledger: spend is written to ``cost_entries`` only by ``spill``, in its own transaction, after the
business transaction that made the call has committed *or* rolled back (``tenant_session`` hooks).
``cost_entries.reservation_id`` is unique, so each reservation becomes spend exactly once, and no
savepoint or rollback in business code can ever lose or duplicate spend. Until it is spilled, an
incurred reservation counts as in-flight exposure. ``reconcile_expired`` covers crashed workers.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import db
from .budget import BudgetExceeded, PaceDeferred, cost_of, month_start, next_day_start, pace_limit
from .models import BudgetLock, BudgetReservation, CostEntry, Tenant, new_id, utcnow

ACTIVE = ("reserved", "incurred")
RESERVATION_TTL = timedelta(minutes=30)


@dataclass
class _Res:
    id: str
    tenant_id: str
    agent_id: str
    task_id: str | None
    estimate: Decimal
    actual: Decimal | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    status: str = "reserved"
    expires_at: datetime = field(default_factory=lambda: utcnow() + RESERVATION_TTL)

    @property
    def exposure(self) -> Decimal:
        return self.actual if self.actual is not None else self.estimate


def _committed(conn, tenant_id: str | None, agent_ids: list[str] | None = None) -> Decimal:
    """Ledger spend this month for a tenant (``None``: every tenant, for the installation cap)."""
    q = select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(CostEntry.created_at >= month_start())
    if tenant_id is not None:
        q = q.where(CostEntry.tenant_id == tenant_id)
    if agent_ids is not None:
        q = q.where(CostEntry.agent_id.in_(agent_ids))
    return Decimal(str(conn.execute(q).scalar_one()))


def _hard_cap() -> Decimal | None:
    from .config import get_settings

    return get_settings().hard_monthly_cap_usd


# Arbitrary constant key for the installation-wide advisory lock (PostgreSQL), taken before any tenant lock.
INSTALLATION_LOCK_KEY = 7_461_001


def _admit(*, org_budget: Decimal, agent_budget: Decimal, agent_name: str, committed_org: Decimal,
           committed_agent: Decimal, active_org: Decimal, active_agent: Decimal, estimate: Decimal,
           installation_spent: Decimal | None = None) -> None:
    cap = _hard_cap()
    if cap is not None and installation_spent is not None and installation_spent + estimate > cap:
        raise BudgetExceeded(
            f"installation hard cap ${cap}/month would be exceeded (spent and in flight across all "
            f"organisations ${installation_spent:.4f}, call estimate ${estimate:.4f})")
    if committed_agent + active_agent + estimate > agent_budget:
        raise BudgetExceeded(
            f"agent {agent_name} budget ${agent_budget} would be exceeded (spent ${committed_agent:.4f}, "
            f"in flight ${active_agent:.4f}, call estimate ${estimate:.4f})")
    if committed_org + active_org + estimate > org_budget:
        raise BudgetExceeded(
            f"organisation budget ${org_budget} would be exceeded (spent ${committed_org:.4f}, "
            f"in flight ${active_org:.4f}, call estimate ${estimate:.4f})")
    now = utcnow()
    paced = pace_limit(org_budget, now)
    if paced is not None and committed_org + active_org + estimate > paced:
        raise PaceDeferred(
            f"spending pace: ${paced:.2f} of the ${org_budget} monthly budget is available so far this month "
            f"(spent ${committed_org:.4f}, in flight ${active_org:.4f}, call estimate ${estimate:.4f}); "
            "the work resumes tomorrow", retry_at=next_day_start(now))


def _write_ledger(tenant_id: str, rows: list[_Res], conn=None) -> None:
    """Record incurred reservations in the ledger (idempotent). With ``conn`` the inserts join the
    caller's transaction, so the ledger rows and the reservation status change commit together."""
    if not rows:
        return
    if conn is None:
        with db.write_transaction() as own:
            _write_ledger(tenant_id, rows, own)
        return
    for r in rows:
        exists = conn.execute(select(CostEntry.id).where(CostEntry.reservation_id == r.id)).first()
        if exists:
            continue
        try:
            with conn.begin_nested():
                conn.execute(insert(CostEntry).values(
                    id=new_id(), tenant_id=tenant_id, agent_id=r.agent_id, task_id=r.task_id,
                    reservation_id=r.id, model=r.model or "unknown", input_tokens=r.input_tokens,
                    output_tokens=r.output_tokens, cost_usd=r.actual or Decimal("0"), created_at=utcnow()))
        except IntegrityError:
            pass  # a concurrent reconciler recorded it first: exactly once either way


class MemoryReservationStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._res: dict[str, _Res] = {}

    def reserve(self, *, tenant_id, agent_id, agent_name, agent_budget, task_id, estimate,
                lineage_ids: list[str] | None = None) -> str:
        """``lineage_ids``: every version of the agent. The agent limit covers the whole lineage, so a
        revision (new id) cannot reset the month's spend."""
        ids = set(lineage_ids or []) | {agent_id}
        with self._lock:
            now = utcnow()
            active = [r for r in self._res.values() if r.tenant_id == tenant_id and r.status in ACTIVE
                      and r.expires_at > now]
            with db.engine().connect() as conn:  # committed data only (separate connection)
                org_budget = Decimal(str(conn.execute(select(Tenant.monthly_budget_usd)
                                                      .where(Tenant.id == tenant_id)).scalar_one()))
                committed_org = _committed(conn, tenant_id)
                committed_agent = _committed(conn, tenant_id, sorted(ids))
                installation = None
                if _hard_cap() is not None:
                    installation = _committed(conn, None) + sum(
                        (r.exposure for r in self._res.values() if r.status in ACTIVE and r.expires_at > now),
                        Decimal("0"))
            _admit(org_budget=org_budget, agent_budget=agent_budget, agent_name=agent_name,
                   committed_org=committed_org, committed_agent=committed_agent,
                   active_org=sum((r.exposure for r in active), Decimal("0")),
                   active_agent=sum((r.exposure for r in active if r.agent_id in ids), Decimal("0")),
                   estimate=estimate, installation_spent=installation)
            r = _Res(new_id(), tenant_id, agent_id, task_id, estimate)
            self._res[r.id] = r
            return r.id

    def incur(self, rid, *, model, input_tokens, output_tokens, cache_write_tokens=0,
              cache_read_tokens=0) -> Decimal:
        with self._lock:
            r = self._res[rid]
            r.model, r.output_tokens = model, output_tokens
            r.input_tokens = input_tokens + cache_write_tokens + cache_read_tokens  # all input, for reporting
            r.actual = cost_of(model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens)
            r.status = "incurred"
            return r.actual

    def release(self, rid) -> None:
        with self._lock:
            if rid in self._res and self._res[rid].status == "reserved":
                self._res[rid].status = "released"

    def settle(self, rids) -> None:
        with self._lock:
            for rid in rids:
                if rid in self._res and self._res[rid].status in ACTIVE:
                    self._res[rid].status = "settled" if self._res[rid].status == "incurred" else "released"
            self._gc()

    def spill(self, rids) -> None:
        # The lock is held across the ledger write AND the settle, so no concurrent reserve() can see
        # the spend twice (ledger + still-active reservation) and wrongly refuse a call.
        with self._lock:
            rows = [self._res[r] for r in rids if r in self._res and self._res[r].status in ACTIVE]
            by_tenant: dict[str, list[_Res]] = {}
            for r in rows:
                if r.status == "incurred":
                    by_tenant.setdefault(r.tenant_id, []).append(r)
            for tid, items in by_tenant.items():
                _write_ledger(tid, items)
            for r in rows:
                r.status = "settled" if r.status == "incurred" else "released"
            self._gc()

    def reconcile_expired(self) -> int:
        now = utcnow()
        with self._lock:
            expired = [r.id for r in self._res.values() if r.status in ACTIVE and r.expires_at <= now]
        self.spill(expired)
        return len(expired)

    def _gc(self) -> None:
        for rid in [k for k, v in self._res.items() if v.status in ("settled", "released")]:
            del self._res[rid]

    def active_count(self) -> int:
        return sum(1 for r in self._res.values() if r.status in ACTIVE)


class SqlReservationStore:
    """Reservations as committed rows, serialised per tenant with a row lock (PostgreSQL)."""

    def _lock_tenant(self, conn, tenant_id: str) -> None:
        conn.execute(pg_insert(BudgetLock).values(id=new_id(), tenant_id=tenant_id)
                     .on_conflict_do_nothing(index_elements=["tenant_id"]))
        conn.execute(select(BudgetLock.id).where(BudgetLock.tenant_id == tenant_id).with_for_update())

    def reserve(self, *, tenant_id, agent_id, agent_name, agent_budget, task_id, estimate,
                lineage_ids: list[str] | None = None) -> str:
        ids = sorted(set(lineage_ids or []) | {agent_id})
        now = utcnow()
        with db.write_transaction() as conn:
            capped = _hard_cap() is not None
            if capped:  # installation lock first, then the tenant lock: one global order, no deadlock
                conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": INSTALLATION_LOCK_KEY})
            self._lock_tenant(conn, tenant_id)
            org_budget = Decimal(str(conn.execute(select(Tenant.monthly_budget_usd)
                                                  .where(Tenant.id == tenant_id)).scalar_one()))
            exposure = func.coalesce(BudgetReservation.actual_usd, BudgetReservation.estimate_usd)
            base = select(func.coalesce(func.sum(exposure), 0)).where(
                BudgetReservation.tenant_id == tenant_id, BudgetReservation.status.in_(ACTIVE),
                BudgetReservation.expires_at > now)
            active_org = Decimal(str(conn.execute(base).scalar_one()))
            active_agent = Decimal(str(conn.execute(base.where(BudgetReservation.agent_id.in_(ids))).scalar_one()))
            installation = None
            if capped:
                every = select(func.coalesce(func.sum(exposure), 0)).where(
                    BudgetReservation.status.in_(ACTIVE), BudgetReservation.expires_at > now)
                # Read in-flight BEFORE the ledger (as for the tenant figures above): a spill committing in
                # between is then counted twice (safe), never missed. The reverse order could miss it.
                in_flight_all = Decimal(str(conn.execute(every).scalar_one()))
                installation = in_flight_all + _committed(conn, None)
            _admit(org_budget=org_budget, agent_budget=agent_budget, agent_name=agent_name,
                   committed_org=_committed(conn, tenant_id), committed_agent=_committed(conn, tenant_id, ids),
                   active_org=active_org, active_agent=active_agent, estimate=estimate,
                   installation_spent=installation)
            rid = new_id()
            conn.execute(insert(BudgetReservation).values(
                id=rid, tenant_id=tenant_id, agent_id=agent_id, task_id=task_id, estimate_usd=estimate,
                status="reserved", created_at=now, expires_at=now + RESERVATION_TTL))
            return rid

    def incur(self, rid, *, model, input_tokens, output_tokens, cache_write_tokens=0,
              cache_read_tokens=0) -> Decimal:
        actual = cost_of(model, input_tokens, output_tokens, cache_write_tokens, cache_read_tokens)
        with db.write_transaction() as conn:
            conn.execute(update(BudgetReservation).where(BudgetReservation.id == rid).values(
                actual_usd=actual, model=model, input_tokens=input_tokens + cache_write_tokens + cache_read_tokens,
                output_tokens=output_tokens,
                status="incurred"))
        return actual

    def release(self, rid) -> None:
        with db.write_transaction() as conn:
            conn.execute(update(BudgetReservation).where(BudgetReservation.id == rid,
                                                         BudgetReservation.status == "reserved")
                         .values(status="released"))

    def settle(self, rids) -> None:
        if not rids:
            return
        with db.write_transaction() as conn:
            conn.execute(update(BudgetReservation).where(BudgetReservation.id.in_(rids),
                                                         BudgetReservation.status == "incurred")
                         .values(status="settled"))
            conn.execute(update(BudgetReservation).where(BudgetReservation.id.in_(rids),
                                                         BudgetReservation.status == "reserved")
                         .values(status="released"))

    def spill(self, rids) -> None:
        if not rids:
            return
        t = BudgetReservation.__table__
        # One transaction: ledger rows + reservation status change commit atomically. It also takes the
        # same locks as reserve() (installation lock first, then tenant locks in sorted order), so a
        # reserve() never reads the two halves of a spill in between and sees each dollar exactly once.
        with db.write_transaction() as conn:
            tenant_ids = sorted({tid for (tid,) in conn.execute(select(t.c.tenant_id).where(t.c.id.in_(rids)))})
            if _hard_cap() is not None:
                conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": INSTALLATION_LOCK_KEY})
            for tid in tenant_ids:
                self._lock_tenant(conn, tid)
            rows = conn.execute(select(t).where(t.c.id.in_(rids), t.c.status == "incurred")
                                .with_for_update()).all()
            by_tenant: dict[str, list[_Res]] = {}
            for row in rows:
                r = _Res(row.id, row.tenant_id, row.agent_id, row.task_id, row.estimate_usd, row.actual_usd,
                         row.model, row.input_tokens, row.output_tokens, row.status)
                by_tenant.setdefault(r.tenant_id, []).append(r)
            for tid, items in by_tenant.items():
                _write_ledger(tid, items, conn)
            conn.execute(update(BudgetReservation).where(BudgetReservation.id.in_(rids),
                                                         BudgetReservation.status == "incurred")
                         .values(status="settled"))
            conn.execute(update(BudgetReservation).where(BudgetReservation.id.in_(rids),
                                                         BudgetReservation.status == "reserved")
                         .values(status="released"))

    def reconcile_expired(self) -> int:
        with db.engine().connect() as conn:
            ids = [i for (i,) in conn.execute(select(BudgetReservation.id).where(
                BudgetReservation.status.in_(ACTIVE), BudgetReservation.expires_at <= utcnow())).all()]
        self.spill(ids)
        return len(ids)

    def active_count(self) -> int:
        with db.engine().connect() as conn:
            return conn.execute(select(func.count(BudgetReservation.id))
                                .where(BudgetReservation.status.in_(ACTIVE))).scalar_one()


_store = None
_store_lock = threading.Lock()


def store():
    global _store
    with _store_lock:
        dialect = db.engine().dialect.name
        if _store is None or getattr(_store, "_dialect", None) != (dialect, id(db.engine())):
            _store = SqlReservationStore() if dialect == "postgresql" else MemoryReservationStore()
            _store._dialect = (dialect, id(db.engine()))
        return _store


# Session integration: reservations made while a business session is open are settled after
# it commits and spilled to the ledger after it rolls back (see db.tenant_session).
SESSION_KEY = "wwos_reservations"


def track(session, rid: str) -> None:
    session.info.setdefault(SESSION_KEY, []).append(rid)


def after_commit(session) -> None:
    rids = session.info.pop(SESSION_KEY, [])
    if rids:
        store().spill(rids)


def after_rollback(session) -> None:
    rids = session.info.pop(SESSION_KEY, [])
    if rids:
        store().spill(rids)

