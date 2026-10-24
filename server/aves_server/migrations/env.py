"""Alembic environment. Runs on the connection that Database.migrate() hands over (see db.py)."""

from alembic import context

from aves_server.models import Base


target_metadata = Base.metadata
connection = context.config.attributes["connection"]
context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=connection.dialect.name == "sqlite")
with context.begin_transaction():
    context.run_migrations()
