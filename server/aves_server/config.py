from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServerConfig:
    """Deployment settings, read from environment variables.

    User-tunable workflow settings (thresholds, publish pattern, ...) live in the
    database instead, see settings.py, so they can be changed from the UI.
    """

    database_url: str
    dump_dir: Path
    published_dir: Path
    data_dir: Path
    password: str | None

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def models_dir(self) -> Path:
        return Path(os.environ.get("AVES_MODELS_DIR") or self.data_dir / "models")

    @staticmethod
    def from_env() -> "ServerConfig":
        data_dir = Path(os.environ.get("AVES_DATA_DIR", "/data/appdata"))
        return ServerConfig(
            database_url=os.environ.get("AVES_DATABASE_URL", f"sqlite:///{data_dir / 'aves.db'}"),
            dump_dir=Path(os.environ.get("AVES_DUMP_DIR", "/data/dumpsite")),
            published_dir=Path(os.environ.get("AVES_PUBLISHED_DIR", "/data/published")),
            data_dir=data_dir,
            password=os.environ.get("AVES_PASSWORD") or None,
        )
