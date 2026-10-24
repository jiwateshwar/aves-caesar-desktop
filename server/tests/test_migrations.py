from __future__ import annotations

import importlib
import json
from datetime import date, datetime, timezone

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from aves_server.db import Database
from aves_server.models import (Base, Detection, DetectionSource, DetectionStatus, FileStatus, LibraryFile, MediaFile, Session,
                                Shot, ShotStatus, Species)


def _models_match(database: Database) -> list:
    with database.engine.connect() as connection:
        return compare_metadata(MigrationContext.configure(connection), Base.metadata)


def test_a_new_database_built_by_migrations_matches_the_models(database):
    assert _models_match(database) == []
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0003"


def _legacy_database(url: str) -> Database:
    """A database as the first release created it: create_all plus the original views, no alembic_version."""
    database = Database(url)
    baseline = importlib.import_module("aves_server.migrations.versions.0001_baseline")
    with database.engine.begin() as connection:
        Base.metadata.create_all(connection)
        for name, body in baseline.BASELINE_VIEWS.items():
            connection.execute(text(f"CREATE VIEW {name} AS {body}"))
    return database


def test_a_database_from_before_migrations_keeps_its_data_and_gains_the_new_columns(config):
    database = _legacy_database(config.database_url)
    now = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)
    with database.session() as db:
        db.add(Species(species_id="264", scientific_name="Alcedo atthis", common_name="Common Kingfisher", names={"en": "Common Kingfisher", "fr": "Martin-pêcheur d'Europe"}))
        session = Session(shoot_date=date(2026, 10, 18), title="Wimbledon Common")
        shot = Shot(session=session, dump_folder="card", group_key="card|dsc_1|2026-10-18T07:31:02", captured_at=datetime(2026, 10, 18, 7, 31, 2),
                    status=ShotStatus.published, city="Roehampton")
        shot.detections.append(Detection(species_id="264", confidence=0.998, source=DetectionSource.model, status=DetectionStatus.confirmed))
        for name, relpath in (("DSC_1.JPG", "UK/2026-10-18/JPG/DSC_1.JPG"), ("DSC_1.NEF", "UK/2026-10-18/NEF/DSC_1.NEF")):
            db.add(MediaFile(shot=shot, original_filename=name, final_filename=name, filename_key=name.casefold(), extension=name[-4:].lower(),
                             file_type=name[-3:], size_bytes=1, mtime=0, sha256=name, status=FileStatus.published, published_relpath=relpath, published_at=now))
        db.add(LibraryFile(relpath="UK/2026-10-18/JPG/DSC_1.JPG", filename_key="dsc_1.jpg"))  # the NEF was deleted from the library by hand
    assert "alembic_version" not in inspect(database.engine).get_table_names()

    database.migrate()

    assert _models_match(database) == []
    with database.engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT filename, common_name, session_title, species_names, in_library, changed_at FROM v_published_photos ORDER BY filename")).all()
    assert [(r.filename, r.common_name, r.session_title, bool(r.in_library)) for r in rows] == [
        ("DSC_1.JPG", "Common Kingfisher", "Wimbledon Common", True),
        ("DSC_1.NEF", "Common Kingfisher", "Wimbledon Common", False),
    ]
    names = rows[0].species_names if isinstance(rows[0].species_names, dict) else json.loads(rows[0].species_names)
    assert names["fr"] == "Martin-pêcheur d'Europe"
    assert rows[0].changed_at is not None


def test_migrating_twice_is_harmless(database):
    database.migrate()
    assert _models_match(database) == []
