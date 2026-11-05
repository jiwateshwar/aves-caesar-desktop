"""Several dumpsites: a dumpsites table, with the existing one as dumpsite 1.

Existing shots and files are all from the original dumpsite, so their new dumpsite_id is 1.
The columns have no foreign key constraint: SQLite can't add one to an existing table with a default.

Revision ID: 0006
Revises: 0005
"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dumpsites = op.create_table(
        "dumpsites",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slot", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("slot"),
    )
    op.bulk_insert(dumpsites, [{"id": 1, "name": "Dumpsite", "slot": None, "enabled": True,
                               "created_at": datetime.now(timezone.utc)}])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SELECT setval(pg_get_serial_sequence('dumpsites', 'id'), 1)")
    for table in ("shots", "media_files"):
        op.add_column(table, sa.Column("dumpsite_id", sa.Integer(), nullable=False, server_default="1"))
        op.create_index(f"ix_{table}_dumpsite_id", table, ["dumpsite_id"])


def downgrade() -> None:
    for table in ("media_files", "shots"):
        op.drop_index(f"ix_{table}_dumpsite_id", table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_column("dumpsite_id")
    op.drop_table("dumpsites")
