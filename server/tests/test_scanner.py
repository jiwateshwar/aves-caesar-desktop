from __future__ import annotations

import shutil
from datetime import date, datetime

from sqlalchemy import select

from aves_server.models import FileStatus, MediaFile, Session, Shot, ShotStatus


def _settle(scanner, clock):
    """A file is only ingested once it has stopped changing for settle_seconds (30s by default)."""
    first = scanner.scan()
    clock.advance(31)
    return first, scanner.scan()


def test_new_photo_waits_until_it_stops_changing(scanner, clock, config, make_photo, database):
    make_photo(config.dump_dir / "card1" / "DSC_0001.JPG", gps=(11.41, 76.70))

    first, second = _settle(scanner, clock)

    assert (first.waiting, first.ingested) == (1, 0)
    assert (second.waiting, second.ingested) == (0, 1)


def test_manual_scan_ingests_files_that_hold_still(scanner, config, make_photo):
    make_photo(config.dump_dir / "DSC_0001.JPG")

    report = scanner.scan(quick=True)

    assert report.ingested == 1


def test_photo_with_gps_is_located_and_queued(scanner, clock, config, make_photo, database):
    make_photo(config.dump_dir / "card1" / "DSC_0001.JPG", gps=(11.41, 76.70))
    _settle(scanner, clock)

    with database.session() as db:
        shot = db.scalar(select(Shot))
        file = shot.files[0]
        assert shot.session.shoot_date == date(2026, 10, 18)
        assert shot.status == ShotStatus.pending
        assert (shot.country, shot.state, shot.city, shot.location_source) == ("India", "Tamil Nadu", "Ooty", "exif")
        assert shot.dump_folder == "card1"
        assert (file.camera_make, file.camera_model, file.iso, file.f_number) == ("Nikon", "Z 8", 1600, 6.3)
        assert file.exif["EXIF:LensModel"] == "NIKKOR Z 180-600mm"
        assert "SourceFile" not in file.exif and "File:Directory" not in file.exif


def test_photo_without_gps_waits_for_a_session_location(scanner, clock, config, make_photo, database):
    make_photo(config.dump_dir / "DSC_0002.JPG")
    _settle(scanner, clock)

    with database.session() as db:
        assert db.scalar(select(Shot)).status == ShotStatus.awaiting_location


def test_photos_are_grouped_into_one_session_per_shooting_day(scanner, clock, config, make_photo, database):
    make_photo(config.dump_dir / "a" / "1.JPG", taken=datetime(2026, 10, 18, 6, 0))
    make_photo(config.dump_dir / "b" / "2.JPG", taken=datetime(2026, 10, 18, 18, 0))
    make_photo(config.dump_dir / "a" / "3.JPG", taken=datetime(2026, 10, 19, 6, 0))
    _settle(scanner, clock)

    with database.session() as db:
        sessions = {s.shoot_date: len(s.shots) for s in db.scalars(select(Session))}
        assert sessions == {date(2026, 10, 18): 2, date(2026, 10, 19): 1}


def test_raw_and_jpeg_of_the_same_frame_become_one_shot(scanner, clock, config, make_photo, database):
    make_photo(config.dump_dir / "DSC_0003.JPG")
    make_photo(config.dump_dir / "DSC_0003.TIF")  # stands in for the RAW; same stem and capture time
    make_photo(config.dump_dir / "DSC_0004.JPG", taken=datetime(2026, 10, 18, 7, 12, 40))
    _settle(scanner, clock)

    with database.session() as db:
        shots = list(db.scalars(select(Shot).order_by(Shot.id)))
        assert sorted(f.file_type for f in shots[0].files) == ["JPG", "TIFF"]
        assert len(shots) == 2


def test_identical_file_dumped_twice_is_flagged_as_duplicate(scanner, clock, config, make_photo, database):
    original = make_photo(config.dump_dir / "day1" / "DSC_0005.JPG")
    (config.dump_dir / "day1-again").mkdir()
    shutil.copy2(original, config.dump_dir / "day1-again" / "DSC_0005.JPG")
    _first, report = _settle(scanner, clock)

    assert (report.ingested, report.duplicates) == (1, 1)
    with database.session() as db:
        duplicate = db.scalar(select(MediaFile).where(MediaFile.status == FileStatus.duplicate))
        assert duplicate.shot_id is None and duplicate.duplicate_of_id is not None


def test_videos_and_nas_clutter_are_ignored(scanner, clock, config, make_photo):
    (config.dump_dir / "clip.MP4").write_bytes(b"\x00" * 100)
    (config.dump_dir / "@eaDir").mkdir()
    make_photo(config.dump_dir / "@eaDir" / "thumb.JPG")
    make_photo(config.dump_dir / "._DSC_0001.JPG")

    _first, report = _settle(scanner, clock)

    assert (report.found, report.ingested) == (0, 0)


def test_files_deleted_from_the_dump_are_marked_missing(scanner, clock, config, make_photo, database):
    keep = make_photo(config.dump_dir / "keep.JPG")
    gone = make_photo(config.dump_dir / "gone.JPG", color=(1, 2, 3))
    _settle(scanner, clock)
    gone.unlink()

    report = scanner.scan()

    assert report.missing == 1 and keep.exists()


def test_empty_dumpsite_is_treated_as_unmounted_not_deleted(scanner, clock, config, make_photo, database):
    make_photo(config.dump_dir / "DSC_0001.JPG")
    _settle(scanner, clock)
    (config.dump_dir / "DSC_0001.JPG").unlink()

    report = scanner.scan()

    assert report.missing == 0
    with database.session() as db:
        assert db.scalar(select(MediaFile)).status == FileStatus.in_dump
