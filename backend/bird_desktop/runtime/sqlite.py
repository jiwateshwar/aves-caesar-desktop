import sqlite3


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback AND close; sqlite3's default context only does the former."""

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()
