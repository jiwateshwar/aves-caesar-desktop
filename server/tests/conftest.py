from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import text

from bird_desktop.media.exiftool import ExifTool
from bird_desktop.media.exiftool_session import ExifToolSession

from aves_server.config import ServerConfig
from aves_server.db import Database
from aves_server.media import MetadataReader
from aves_server.models import Base
from aves_server.scanner import DumpScanner
from aves_server.views import VIEWS


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def config(tmp_path: Path) -> ServerConfig:
    for name in ("dump", "published", "appdata"):
        (tmp_path / name).mkdir()
    # AVES_TEST_DATABASE_URL=postgresql+psycopg://... runs the suite against PostgreSQL (wiped per test).
    url = os.environ.get("AVES_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'appdata' / 'test.db'}"
    return ServerConfig(database_url=url, dump_dir=tmp_path / "dump",
                        published_dir=tmp_path / "published", data_dir=tmp_path / "appdata", password=None)


@pytest.fixture
def database(config: ServerConfig) -> Database:
    database = Database(config.database_url)
    if not config.database_url.startswith("sqlite"):
        with database.engine.begin() as connection:
            for view in VIEWS:
                connection.execute(text(f"DROP VIEW IF EXISTS {view}"))
        Base.metadata.drop_all(database.engine)
    database.create_schema()
    yield database
    database.engine.dispose()


@pytest.fixture(scope="session")
def exiftool_path() -> str:
    executable = ExifTool.find()
    if executable is None:
        pytest.skip("ExifTool is not installed")
    return executable


@pytest.fixture
def exiftool(exiftool_path: str):
    with ExifToolSession(exiftool_path) as session:
        yield session


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def scanner(config, database, exiftool, clock) -> DumpScanner:
    return DumpScanner(config, database, MetadataReader(exiftool), clock=clock, sleep=lambda _seconds: None)


@pytest.fixture
def make_photo(exiftool_path: str):
    """Writes a small image with real EXIF tags, the way a camera would."""

    def make(path: Path, taken: datetime | None = datetime(2026, 10, 18, 7, 12, 33), gps: tuple[float, float] | None = None,
             color=(110, 150, 90), fmt: str | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (96, 64), color).save(path, fmt or ("TIFF" if path.suffix.lower() in (".tif", ".tiff") else "JPEG"))
        tags = ["-Make=Nikon", "-Model=Z 8", "-LensModel=NIKKOR Z 180-600mm", "-ISO=1600", "-FNumber=6.3", "-ExposureTime=0.001"]
        if taken is not None:
            tags.append(f"-DateTimeOriginal={taken:%Y:%m:%d %H:%M:%S}")
        if gps is not None:
            lat, lon = gps
            tags += [f"-GPSLatitude={abs(lat)}", f"-GPSLatitudeRef={'S' if lat < 0 else 'N'}",
                     f"-GPSLongitude={abs(lon)}", f"-GPSLongitudeRef={'W' if lon < 0 else 'E'}"]
        subprocess.run([exiftool_path, "-q", "-overwrite_original", *tags, str(path)], check=True)
        return path

    return make
