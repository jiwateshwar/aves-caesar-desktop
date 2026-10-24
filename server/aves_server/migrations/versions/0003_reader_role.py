"""Read-only access for other applications: the featherbase_reader group role (PostgreSQL only).

It can read the published-photo views and the species list, nothing else. Views run with their
owner's rights, so the role needs no access to the underlying tables. Login users for other apps are
created separately (they have passwords) and made members of this role, e.g.:

    CREATE ROLE featherbase_public LOGIN PASSWORD '...' IN ROLE featherbase_reader;

Grants survive CREATE OR REPLACE VIEW. A later migration that DROPs one of these views must grant
SELECT on it again.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

READABLE = ("v_published_photos", "v_species_sightings", "species")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'featherbase_reader') THEN
                CREATE ROLE featherbase_reader NOLOGIN;
            END IF;
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO featherbase_reader', current_database());
        END
        $$
    """)
    op.execute("GRANT USAGE ON SCHEMA public TO featherbase_reader")
    op.execute(f"GRANT SELECT ON {', '.join(READABLE)} TO featherbase_reader")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    # The role itself is kept: login users of other apps may still be members of it.
    op.execute(f"REVOKE SELECT ON {', '.join(READABLE)} FROM featherbase_reader")
    op.execute("REVOKE USAGE ON SCHEMA public FROM featherbase_reader")
