"""Shared helpers for migrations (views differ between PostgreSQL and SQLite)."""

from alembic import op
from sqlalchemy import text


def replace_view(name: str, body: str) -> None:
    """CREATE OR REPLACE on PostgreSQL keeps grants (e.g. the read-only role's); SQLite has no OR REPLACE."""
    if op.get_bind().dialect.name == "postgresql":
        op.execute(text(f"CREATE OR REPLACE VIEW {name} AS {body}"))
    else:
        op.execute(text(f"DROP VIEW IF EXISTS {name}"))
        op.execute(text(f"CREATE VIEW {name} AS {body}"))


def drop_view(name: str) -> None:
    op.execute(text(f"DROP VIEW IF EXISTS {name}"))
