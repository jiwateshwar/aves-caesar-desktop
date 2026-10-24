from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text

from aves_server.analysis import Analyzer
from aves_server.catalog import SpeciesCatalog
from aves_server.locations import set_analyse_without_location, set_session_location
from aves_server.models import DetectionStatus, FileStatus, LibraryFile, MediaFile, Shot, ShotStatus
from aves_server.publisher import ConflictChecker, LibraryIndexer, Publisher, target_folder
from aves_server.review import (ReviewError, add_species, append_suffix, confirm_confident, confirm_shot, ignore_shot,
                                reject_detection, set_species)
from aves_server.settings import SettingsStore, WorkflowSettings


SPECIES_CSV = Path(__file__).parent / "data" / "species.csv"


@pytest.fixture
def catalog(database) -> SpeciesCatalog:
    catalog = SpeciesCatalog(SPECIES_CSV)
    with database.session() as db:
        catalog.seed(db)
    return catalog


def _bird(species_id: str, confidence: float, alternatives=(("103", 0.05),)):
    return {"box": [10, 10, 50, 40], "box_confidence": 0.93,
            "classification": [{"species_id": species_id, "confidence": confidence}] + [{"species_id": s, "confidence": c} for s, c in alternatives]}


def _ingest(scanner, clock):
    scanner.scan()
    clock.advance(31)
    return scanner.scan()


def _analyse(database, birds_by_name: dict[str, list[dict]]):
    """Stores model output as the Analyzer would, without loading the 1.8GB models."""
    with database.session() as db:
        for shot in db.scalars(select(Shot).where(Shot.status == ShotStatus.pending)):
            name = shot.files[0].original_filename
            Analyzer._store(shot, birds_by_name.get(name, []), (96, 64), shot.location_source is not None, ["CUDAExecutionProvider"])


def test_setting_a_session_location_locates_and_queues_its_photos(scanner, clock, config, make_photo, database):
    make_photo(config.dump_dir / "DSC_0001.JPG")
    _ingest(scanner, clock)

    with database.session() as db:
        shot = db.scalar(select(Shot))
        queued = set_session_location(shot.session, 51.5007, -0.1246, None, WorkflowSettings())
        assert queued == 1
        assert shot.status == ShotStatus.pending
        assert (shot.country, shot.state, shot.location_source) == ("United Kingdom", "England", "session")
        assert shot.city == shot.session.city and shot.session.location_label.startswith(shot.city)


def test_analyse_without_location_releases_waiting_photos(scanner, clock, config, make_photo, database):
    make_photo(config.dump_dir / "DSC_0001.JPG")
    _ingest(scanner, clock)

    with database.session() as db:
        shot = db.scalar(select(Shot))
        assert set_analyse_without_location(shot.session, True) == 1
        assert shot.status == ShotStatus.pending


def test_reviewing_a_shot(scanner, clock, config, make_photo, database, catalog):
    make_photo(config.dump_dir / "DSC_0001.JPG", gps=(11.41, 76.70))
    _ingest(scanner, clock)
    _analyse(database, {"DSC_0001.JPG": [_bird("101", 0.42, (("102", 0.40),)), _bird("103", 0.9)]})

    with database.session() as db:
        shot = db.scalar(select(Shot))
        assert (shot.status, shot.bird_count, shot.min_confidence) == (ShotStatus.review, 2, 0.42)
        first, second = shot.detections
        set_species(shot, first.id, "102", catalog)        # pick the model's second choice
        assert (first.species_id, first.confidence) == ("102", 0.40)
        reject_detection(shot, second.id)                  # not actually a bird
        confirm_shot(shot)
        assert shot.status == ShotStatus.confirmed and shot.bird_count == 1
        assert [d.status for d in shot.detections] == [DetectionStatus.confirmed, DetectionStatus.rejected]
        with pytest.raises(ReviewError):
            set_species(shot, first.id, "999", catalog)


def test_a_no_bird_photo_can_be_tagged_published_or_left(scanner, clock, config, make_photo, database, catalog):
    make_photo(config.dump_dir / "empty.JPG", gps=(11.41, 76.70))
    make_photo(config.dump_dir / "landscape.JPG", gps=(11.41, 76.70), taken=datetime(2026, 10, 18, 9, 0))
    _ingest(scanner, clock)
    _analyse(database, {})

    with database.session() as db:
        tagged, left = db.scalars(select(Shot).order_by(Shot.id)).all()
        assert tagged.bird_count == 0
        add_species(tagged, "103", catalog)
        confirm_shot(tagged)
        ignore_shot(left)
        assert (tagged.status, tagged.bird_count, left.status) == (ShotStatus.confirmed, 1, ShotStatus.ignored)


def test_bulk_confirm_takes_only_confident_untouched_shots(scanner, clock, config, make_photo, database, catalog):
    for minute, name in enumerate(["sure.JPG", "unsure.JPG", "edited.JPG"]):
        make_photo(config.dump_dir / name, gps=(11.41, 76.70), taken=datetime(2026, 10, 18, 7, minute))
    _ingest(scanner, clock)
    _analyse(database, {"sure.JPG": [_bird("101", 0.97)], "unsure.JPG": [_bird("101", 0.55)], "edited.JPG": [_bird("101", 0.95)]})

    with database.session() as db:
        shots = {s.files[0].original_filename: s for s in db.scalars(select(Shot))}
        set_species(shots["edited.JPG"], shots["edited.JPG"].detections[0].id, "102", catalog)
        assert confirm_confident(db, list(shots.values()), 0.6) == 1
        assert shots["sure.JPG"].status == ShotStatus.confirmed
        assert shots["unsure.JPG"].status == shots["edited.JPG"].status == ShotStatus.review


def test_publish_path_follows_the_pattern(scanner, clock, config, make_photo, database):
    make_photo(config.dump_dir / "DSC_0001.JPG", gps=(11.41, 76.70))
    _ingest(scanner, clock)
    with database.session() as db:
        shot = db.scalar(select(Shot))
        assert target_folder(shot, shot.files[0], WorkflowSettings()).as_posix() == "India/Tamil Nadu/Ooty/2026-10-18/JPG"
        shot.country = shot.state = None
        shot.city = 'Bad/Name: "x".'
        assert target_folder(shot, shot.files[0], WorkflowSettings()).as_posix() == "Unknown/Unknown/Bad-Name- -x-/2026-10-18/JPG"


def _confirmed_pair(scanner, clock, config, make_photo, database, catalog):
    make_photo(config.dump_dir / "card" / "DSC_0001.JPG", gps=(11.41, 76.70))
    make_photo(config.dump_dir / "card" / "DSC_0001.TIF", gps=(11.41, 76.70))
    (config.dump_dir / "card" / "DSC_0001.TIF.xmp").write_text("<x:xmpmeta/>")
    _ingest(scanner, clock)
    _analyse(database, {"DSC_0001.JPG": [_bird("101", 0.97)], "DSC_0001.TIF": [_bird("101", 0.97)]})
    with database.session() as db:
        shot = db.scalar(select(Shot))
        confirm_shot(shot)
        return shot.id


def test_publishing_moves_the_pair_and_its_sidecar_keeping_names(scanner, clock, config, make_photo, database, catalog):
    shot_id = _confirmed_pair(scanner, clock, config, make_photo, database, catalog)

    report = Publisher(config, database).publish([shot_id])

    folder = config.published_dir / "India" / "Tamil Nadu" / "Ooty" / "2026-10-18"
    assert (report.published_shots, report.published_files, report.errors) == (1, 2, [])
    assert (folder / "JPG" / "DSC_0001.JPG").is_file()
    assert (folder / "TIFF" / "DSC_0001.TIF").is_file()
    assert (folder / "TIFF" / "DSC_0001.TIF.xmp").is_file()
    assert not any((config.dump_dir / "card").iterdir())
    with database.session() as db:
        shot = db.get(Shot, shot_id)
        assert shot.status == ShotStatus.published
        assert {f.published_relpath for f in shot.files} == {"India/Tamil Nadu/Ooty/2026-10-18/JPG/DSC_0001.JPG",
                                                             "India/Tamil Nadu/Ooty/2026-10-18/TIFF/DSC_0001.TIF"}
        assert db.query(LibraryFile).count() == 2
        rows = db.execute(text("SELECT filename, common_name, city FROM v_published_photos")).all()
        assert sorted(tuple(row) for row in rows) == [("DSC_0001.JPG", "Verditer Flycatcher", "Ooty"), ("DSC_0001.TIF", "Verditer Flycatcher", "Ooty")]


def test_name_already_in_library_blocks_publishing_until_a_suffix_is_added(scanner, clock, config, make_photo, database, catalog):
    existing = config.published_dir / "Older" / "trip" / "DSC_0001.jpg"   # put there by hand, different case
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"older photo")
    LibraryIndexer(config, database).reindex()
    shot_id = _confirmed_pair(scanner, clock, config, make_photo, database, catalog)

    with database.session() as db:
        reasons = ConflictChecker(db).conflicts()
        assert sorted(reasons.values()) == ["library"]  # only the JPEG clashes

    report = Publisher(config, database).publish([shot_id])
    assert report.published_files == 0 and len(report.conflicts) == 1
    assert (config.dump_dir / "card" / "DSC_0001.TIF").is_file()   # the RAW stays with its JPEG

    with database.session() as db:
        shot = db.get(Shot, shot_id)
        with pytest.raises(ReviewError):
            append_suffix(db, shot, "../evil", ConflictChecker(db))
        assert append_suffix(db, shot, "_trip2", ConflictChecker(db)) == ["DSC_0001_trip2.JPG", "DSC_0001_trip2.TIF"]

    report = Publisher(config, database).publish([shot_id])
    folder = config.published_dir / "India" / "Tamil Nadu" / "Ooty" / "2026-10-18"
    assert report.published_files == 2
    assert (folder / "JPG" / "DSC_0001_trip2.JPG").is_file()
    assert (folder / "TIFF" / "DSC_0001_trip2.TIF.xmp").is_file()
    assert existing.read_bytes() == b"older photo"
    with database.session() as db:
        file = db.scalar(select(MediaFile).where(MediaFile.file_type == "JPG"))
        assert (file.original_filename, file.final_filename, file.status) == ("DSC_0001.JPG", "DSC_0001_trip2.JPG", FileStatus.published)


def test_settings_are_validated(database):
    with database.session() as db:
        assert SettingsStore.update(db, {"confidence_threshold": 0.75}).confidence_threshold == 0.75
        for bad in ({"confidence_threshold": 2}, {"publish_pattern": "{country}/{camera}"}, {"publish_pattern": "../x"}, {"nope": 1}):
            with pytest.raises(ValueError):
                SettingsStore.update(db, bad)
    with database.session() as db:
        assert SettingsStore.load(db).confidence_threshold == 0.75
