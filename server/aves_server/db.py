from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session as OrmSession, sessionmaker

from .models import Base


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

    def create_schema(self) -> None:
        from .views import create_views

        with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                # The web and worker containers start together; only one may create the schema at a time.
                connection.execute(text("SELECT pg_advisory_xact_lock(724514)"))
            Base.metadata.create_all(connection)
            create_views(connection)

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
