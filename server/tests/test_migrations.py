from __future__ import annotations

import json

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from aves_server.db import BASELINE_REVISION, MIGRATIONS, Database
from aves_server.models import Base


def _models_match(database: Database) -> list:
    with database.engine.connect() as connection:
        return compare_metadata(MigrationContext.configure(connection), Base.metadata)


def test_a_new_database_built_by_migrations_matches_the_models(database):
    assert _models_match(database) == []
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0004"


def _legacy_database(url: str) -> Database:
    """A database as the first release created it: the baseline schema and views, without alembic_version."""
    database = Database(url)
    with database.engine.begin() as connection:
        config = Config()
        config.set_main_option("script_location", str(MIGRATIONS))
        config.attributes["connection"] = connection
        command.upgrade(config, BASELINE_REVISION)
        connection.execute(text("DROP TABLE alembic_version"))
    return database


def test_a_database_from_before_migrations_keeps_its_data_and_gains_the_new_columns(config):
    database = _legacy_database(config.database_url)
    now = "2026-10-20 09:00:00"
    # Plain SQL: these are the columns the first release had, not today's models.
    rows = [
        "INSERT INTO species (species_id, scientific_name, common_name, names) VALUES ('264', 'Alcedo atthis', 'Common Kingfisher', "
        "'{\"en\": \"Common Kingfisher\", \"fr\": \"Martin-pêcheur d''Europe\"}')",
        f"INSERT INTO sessions (id, shoot_date, title, analyse_without_location, created_at, updated_at) VALUES (1, '2026-10-18', 'Wimbledon Common', 0, '{now}', '{now}')",
        "INSERT INTO shots (id, session_id, dump_folder, group_key, captured_at, captured_at_source, status, analysis_attempts, bird_count, "
        f"preview_ready, city, created_at, updated_at) VALUES (1, 1, 'card', 'card|dsc_1|x', '2026-10-18 07:31:02', 'exif', 'published', 0, 1, 1, 'Roehampton', '{now}', '{now}')",
        f"INSERT INTO detections (id, shot_id, predictions, species_id, confidence, source, status, created_at, updated_at) VALUES (1, 1, '[]', '264', 0.998, 'model', 'confirmed', '{now}', '{now}')",
    ]
    for file_id, (name, relpath) in enumerate((("DSC_1.JPG", "UK/2026-10-18/JPG/DSC_1.JPG"), ("DSC_1.NEF", "UK/2026-10-18/NEF/DSC_1.NEF")), start=1):
        rows.append("INSERT INTO media_files (id, shot_id, original_filename, final_filename, filename_key, extension, file_type, is_raw, size_bytes, mtime, "
                    f"sha256, status, published_relpath, exif, ingested_at, published_at) VALUES ({file_id}, 1, '{name}', '{name}', '{name.lower()}', "
                    f"'{name[-4:].lower()}', '{name[-3:]}', 0, 1, 0, '{name}', 'published', '{relpath}', '{{}}', '{now}', '{now}')")
    # The NEF was deleted from the library by hand, so only the JPEG is indexed.
    rows.append(f"INSERT INTO library_files (relpath, filename_key, seen_at) VALUES ('UK/2026-10-18/JPG/DSC_1.JPG', 'dsc_1.jpg', '{now}')")
    with database.engine.begin() as connection:
        for sql in rows:
            connection.execute(text(sql))
    assert "alembic_version" not in inspect(database.engine).get_table_names()

    database.migrate()

    assert _models_match(database) == []
    with database.engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT filename, common_name, session_title, species_names, in_library, changed_at, capture_offset_days "
            "FROM v_published_photos ORDER BY filename")).all()
    assert [(r.filename, r.common_name, r.session_title, bool(r.in_library), r.capture_offset_days) for r in rows] == [
        ("DSC_1.JPG", "Common Kingfisher", "Wimbledon Common", True, 0),
        ("DSC_1.NEF", "Common Kingfisher", "Wimbledon Common", False, 0),
    ]
    names = rows[0].species_names if isinstance(rows[0].species_names, dict) else json.loads(rows[0].species_names)
    assert names["fr"] == "Martin-pêcheur d'Europe"
    assert rows[0].changed_at is not None


def test_migrating_twice_is_harmless(database):
    database.migrate()
    assert _models_match(database) == []
