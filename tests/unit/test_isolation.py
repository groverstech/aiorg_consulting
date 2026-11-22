import pytest
from sqlalchemy import select, update

from wwos import crm
from wwos.db import TenantIsolationError, tenant_session
from wwos.models import Agent, Contact


def test_tenant_cannot_read_another_tenants_rows(tenant, other_tenant):
    a, _ = tenant
    b, _ = other_tenant
    with tenant_session(b) as s:
        c, _ = crm.upsert_contact(s, email="secret@other.com", first_name="Sam")
        other_id = c.id
    with tenant_session(a) as s:
        assert s.scalars(select(Contact)).all() == []
        assert s.get(Contact, other_id) is None
        names = {x.name for x in s.scalars(select(Agent))}
        assert names and all(x.tenant_id == a for x in s.scalars(select(Agent)))


def test_tenant_cannot_write_into_another_tenant(tenant, other_tenant):
    a, _ = tenant
    b, _ = other_tenant
    with pytest.raises(TenantIsolationError):
        with tenant_session(a) as s:
            s.add(Contact(tenant_id=b, email="x@y.com", first_name="X"))


def test_bulk_update_is_tenant_filtered(tenant, other_tenant):
    a, _ = tenant
    b, _ = other_tenant
    with tenant_session(b) as s:
        crm.upsert_contact(s, email="keep@other.com", first_name="Keep")
    with tenant_session(a) as s:
        s.execute(update(Contact).values(notes="tampered"))
    with tenant_session(b) as s:
        assert s.scalars(select(Contact)).one().notes != "tampered"


def test_new_rows_are_stamped_with_session_tenant(tenant):
    a, _ = tenant
    with tenant_session(a) as s:
        c = Contact(email="auto@x.com", first_name="Auto")
        s.add(c)
        s.flush()
        assert c.tenant_id == a
