"""Read-only dumpsites: publishing copies their photos instead of moving them.

Revision ID: 0007
Revises: 0006
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dumpsites", sa.Column("read_only", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table("dumpsites") as batch:
        batch.drop_column("read_only")
