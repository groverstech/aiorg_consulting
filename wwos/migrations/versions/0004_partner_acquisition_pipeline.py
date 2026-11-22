"""partner acquisition pipeline; owner confirmation separate from NDA

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-22

Data step: partners previously marked "active" only because an NDA was attested become
"prospect" at stage "nda_requested". Under the new rule only an explicit owner confirmation
(NDA + partnership agreement) makes a partner "confirmed". No data is removed.
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None
destructive = False

def _new_columns() -> list[sa.Column]:
    return [
        sa.Column("category", sa.String(length=30), nullable=False, server_default="other"),
        sa.Column("pipeline_stage", sa.String(length=20), nullable=False, server_default="identified"),
        sa.Column("credentials_verified_by", sa.String(length=120), nullable=True),
        sa.Column("agreement_confirmed_by", sa.String(length=120), nullable=True),
        sa.Column("stage_note", sa.Text(), nullable=False, server_default=""),
    ]


def upgrade() -> None:
    with op.batch_alter_table("partners") as batch:
        for col in _new_columns():
            batch.add_column(col)
    op.execute("UPDATE partners SET status = 'prospect', pipeline_stage = 'nda_requested' WHERE status = 'active'")
    # Server defaults only served the backfill; the application sets values explicitly.
    with op.batch_alter_table("partners") as batch:
        for name in ("category", "pipeline_stage", "stage_note"):
            batch.alter_column(name, server_default=None)


def downgrade() -> None:
    op.execute("UPDATE partners SET status = 'active' WHERE status = 'confirmed'")
    with op.batch_alter_table("partners") as batch:
        for col in reversed(_new_columns()):
            batch.drop_column(col.name)
