from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session as OrmSession, sessionmaker

from .models import Base  # noqa: F401  (imported so the models are registered)


MIGRATIONS = Path(__file__).parent / "migrations"
BASELINE_REVISION = "0001"


class Database:
    def __init__(self, url: str):
        self.url = url
        is_sqlite = url.startswith("sqlite")
        connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
        self.engine: Engine = create_engine(url, connect_args=connect_args, pool_pre_ping=not is_sqlite)
        if is_sqlite:
            event.listen(self.engine, "connect", self._sqlite_pragmas)
        self._factory = sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _sqlite_pragmas(connection, _record) -> None:
        # WAL lets the web and worker processes share one SQLite file in development.
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    def migrate(self) -> None:
        """Brings the schema up to date with the migrations in aves_server/migrations."""
        from alembic import command
        from alembic.config import Config

        with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                # The web and worker containers start together; only one may migrate at a time.
                connection.execute(text("SELECT pg_advisory_xact_lock(724514)"))
            config = Config()
            config.set_main_option("script_location", str(MIGRATIONS))
            config.attributes["connection"] = connection
            tables = set(inspect(connection).get_table_names())
            if "alembic_version" not in tables and "shots" in tables:
                # Created before migrations existed (by create_all): it already matches the baseline.
                command.stamp(config, BASELINE_REVISION)
            command.upgrade(config, "head")

    @contextmanager
    def session(self) -> Iterator[OrmSession]:
        session = self._factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
