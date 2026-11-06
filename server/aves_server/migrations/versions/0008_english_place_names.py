"""Place names in plain English letters ("Bhīm Tāl" -> "Bhim Tal") for sessions and unpublished shots.

Place names now come out of the geocoder this way; this converts names stored before. Published shots are left
alone: their folders already exist on disk under the names they were published with.

Revision ID: 0008
Revises: 0007
"""

from alembic import op
import sqlalchemy as sa

from aves_server.geocoding import english

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _convert(table: str, columns: tuple[str, ...], where: str = "") -> None:
    connection = op.get_bind()
    rows = connection.execute(sa.text(f"SELECT id, {', '.join(columns)} FROM {table} {where}")).all()
    for row in rows:
        values = dict(zip(columns, row[1:]))
        changed = {k: english(v) for k, v in values.items() if v and not v.isascii()}
        if changed:
            assignments = ", ".join(f"{k} = :{k}" for k in changed)
            connection.execute(sa.text(f"UPDATE {table} SET {assignments} WHERE id = :id"), {**changed, "id": row[0]})


def upgrade() -> None:
    _convert("sessions", ("country", "state", "city", "location_label"))
    _convert("shots", ("country", "state", "city"), "WHERE status <> 'published'")


def downgrade() -> None:
    pass  # the original spellings aren't kept; the geocoder would produce the English ones again anyway
