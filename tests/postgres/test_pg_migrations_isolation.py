import pytest
from sqlalchemy import select, text

from wwos import crm, migrate
from wwos.db import TenantIsolationError, tenant_session
from wwos.models import Contact

pytestmark = pytest.mark.postgres


def test_migrations_reach_head_without_drift(pg):
    assert migrate.current() == migrate.head()
    assert migrate.drift() == []


def test_downgrade_and_upgrade_round_trip_preserves_data(pg, tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        crm.upsert_contact(s, email="keep@x.com", first_name="Keep")
    with pytest.raises(migrate.MigrationError):
        migrate.downgrade("0001", allow_destructive=False, backup_confirmed=True)
    assert migrate.downgrade("0001", allow_destructive=True, backup_confirmed=True) == "0001"
    with pytest.raises(migrate.MigrationError, match="backup"):
        migrate.upgrade()  # PostgreSQL requires an explicit backup confirmation
    migrate.upgrade(backup_confirmed=True)
    assert migrate.current() == migrate.head() and migrate.drift() == []
    with tenant_session(tid) as s:
        assert s.scalars(select(Contact)).one().email == "keep@x.com"


def test_isolation_on_postgres(pg, tenant, other_tenant):
    a, _ = tenant
    b, _ = other_tenant
    with tenant_session(b) as s:
        cid = crm.upsert_contact(s, email="secret@b.com", first_name="S")[0].id
    with tenant_session(a) as s:
        assert s.get(Contact, cid) is None
        assert s.scalars(select(Contact)).all() == []
        assert s.execute(text("select 1")).scalar_one() == 1
    with pytest.raises(TenantIsolationError):
        with tenant_session(a) as s:
            s.add(Contact(tenant_id=b, email="x@y.com", first_name="X"))
