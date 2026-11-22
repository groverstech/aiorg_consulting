"""PostgreSQL-backed tests. Enabled by WWOS_TEST_PG_URL (a server URL whose user may create
databases), e.g. postgresql+psycopg://wwos@localhost:5432/postgres. Each test gets a fresh database."""
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from wwos import db

PG_URL = os.environ.get("WWOS_TEST_PG_URL")


@pytest.fixture
def pg(monkeypatch, fresh_db):
    if not PG_URL:
        pytest.skip("WWOS_TEST_PG_URL not set")
    name = f"wwos_t_{uuid.uuid4().hex[:10]}"
    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(PG_URL).set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("WWOS_DATABASE_URL", url)
    db.configure(url)
    db.init_db()
    yield url
    db.engine().dispose()
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()
