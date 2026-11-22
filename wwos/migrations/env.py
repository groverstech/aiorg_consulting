"""Alembic environment. The database URL comes from WWOS_DATABASE_URL (or an explicit
connection passed via config.attributes["connection"] by wwos.migrate)."""
from alembic import context

from wwos import models  # noqa: F401  (register all tables)
from wwos.db import Base, engine

target_metadata = Base.metadata


def run_migrations() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    with engine().connect() as conn:
        _run(conn)
        conn.commit()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite needs table rebuilds for ALTER
        compare_type=True,
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("offline (SQL script) migrations are not supported; run against a database")
run_migrations()
