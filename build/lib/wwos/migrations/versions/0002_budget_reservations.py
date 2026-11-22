"""budget reservations; immutable ledger independent of agent rows

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None
destructive = False

FK_CONVENTION = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}


def _agent_fk_name(bind):
    for fk in sa.inspect(bind).get_foreign_keys("cost_entries"):
        if fk["constrained_columns"] == ["agent_id"]:
            return fk.get("name")
    return None


def upgrade() -> None:
    op.create_table(
        "budget_locks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id"),
    )
    op.create_index("ix_budget_locks_tenant_id", "budget_locks", ["tenant_id"])
    op.create_table(
        "budget_reservations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("estimate_usd", sa.Numeric(precision=14, scale=6), nullable=False),
        sa.Column("actual_usd", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("model", sa.String(length=60), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in ("agent_id", "status", "expires_at", "tenant_id"):
        op.create_index(f"ix_budget_reservations_{col}", "budget_reservations", [col])

    bind = op.get_bind()
    name = _agent_fk_name(bind)
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("cost_entries", naming_convention=FK_CONVENTION) as batch:
            batch.drop_constraint("fk_cost_entries_agent_id_agents", type_="foreignkey")
            batch.add_column(sa.Column("reservation_id", sa.String(length=36), nullable=True))
            batch.create_unique_constraint("uq_cost_entries_reservation_id", ["reservation_id"])
    else:
        if name:
            op.drop_constraint(name, "cost_entries", type_="foreignkey")
        op.add_column("cost_entries", sa.Column("reservation_id", sa.String(length=36), nullable=True))
        op.create_unique_constraint("uq_cost_entries_reservation_id", "cost_entries", ["reservation_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("cost_entries", naming_convention=FK_CONVENTION) as batch:
            batch.drop_constraint("uq_cost_entries_reservation_id", type_="unique")
            batch.drop_column("reservation_id")
            batch.create_foreign_key("fk_cost_entries_agent_id_agents", "agents", ["agent_id"], ["id"])
    else:
        op.drop_constraint("uq_cost_entries_reservation_id", "cost_entries", type_="unique")
        op.drop_column("cost_entries", "reservation_id")
        op.create_foreign_key("cost_entries_agent_id_fkey", "cost_entries", "agents", ["agent_id"], ["id"])
    for col in ("agent_id", "status", "expires_at", "tenant_id"):
        op.drop_index(f"ix_budget_reservations_{col}", table_name="budget_reservations")
    op.drop_table("budget_reservations")
    op.drop_index("ix_budget_locks_tenant_id", table_name="budget_locks")
    op.drop_table("budget_locks")
