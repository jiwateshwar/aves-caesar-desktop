from __future__ import annotations

import subprocess
from datetime import date, datetime

import pytest
from sqlalchemy import select

from aves_server.analysis import Analyzer
from aves_server.dating import model_datetime, shift_session_date
from aves_server.locations import set_session_location
from aves_server.media import MetadataReader, is_lens_code
from aves_server.models import MediaFile, Session, Shot, ShotStatus
from aves_server.review import ReviewError, confirm_shot
from aves_server.settings import WorkflowSettings


def _ingest(scanner, clock):
    scanner.scan()
    clock.advance(31)
    scanner.scan()


def _review(database, names=None):
    """Marks pending shots as analysed with no birds (as the Analyzer would), without loading the models."""
    with database.session() as db:
        for shot in db.scalars(select(Shot).where(Shot.status == ShotStatus.pending)):
            if names is None or shot.files[0].original_filename in names:
                Analyzer._store(shot, [], (96, 64), True, ["CPUExecutionProvider"])


def test_a_whole_wrong_day_moves_with_its_title_and_location(scanner, clock, config, make_photo, database):
    for minute in range(3):
        make_photo(config.dump_dir / "2013_06_15" / f"DSC_{minute}.JPG", taken=datetime(2010, 1, 14, 20, minute))
    _ingest(scanner, clock)
    with database.session() as db:
        session = db.scalar(select(Session))
        session.title = "Amboseli"
        set_session_location(session, -2.65, 37.26, None, WorkflowSettings())
    _review(database)

    with database.session() as db:
        session = db.scalar(select(Session))
        result = shift_session_date(db, session, date(2013, 6, 15), WorkflowSettings())
        shots = list(db.scalars(select(Shot)))
        assert (result.days, result.moved, result.kept_published, result.requeued) == ((date(2013, 6, 15) - date(2010, 1, 14)).days, 3, 0, 3)
        assert result.session_id == session.id and session.shoot_date == date(2013, 6, 15) and session.title == "Amboseli"
        assert [s.captured_at for s in shots] == [datetime(2013, 6, 15, 20, m) for m in range(3)]
        assert all(s.status == ShotStatus.pending and s.captured_at_offset_days == result.days for s in shots)
        assert model_datetime(shots[0]) == "2013:06:15 20:00:00"  # the classifier gets the corrected season


def test_moving_onto_an_existing_day_merges_and_keeps_published_photos(scanner, clock, config, make_photo, database):
    make_photo(config.dump_dir / "real" / "A.JPG", taken=datetime(2013, 6, 15, 9, 0))
    make_photo(config.dump_dir / "wrong" / "B.JPG", taken=datetime(2010, 1, 14, 20, 0))
    make_photo(config.dump_dir / "wrong" / "C.JPG", taken=datetime(2010, 1, 14, 21, 0))
    _ingest(scanner, clock)
    with database.session() as db:
        wrong = db.scalar(select(Session).where(Session.shoot_date == date(2010, 1, 14)))
        set_session_location(wrong, -2.65, 37.26, None, WorkflowSettings())
    _review(database)
    with database.session() as db:
        published = db.scalar(select(Shot).join(Shot.files).where(MediaFile.original_filename == "C.JPG"))
        confirm_shot(published)
        published.status = ShotStatus.published

    with database.session() as db:
        wrong = db.scalar(select(Session).where(Session.shoot_date == date(2010, 1, 14)))
        result = shift_session_date(db, wrong, date(2013, 6, 15), WorkflowSettings())
        real = db.get(Session, result.session_id)
        assert real.shoot_date == date(2013, 6, 15) and (result.moved, result.kept_published) == (1, 1)
        assert sorted(s.files[0].original_filename for s in real.shots) == ["A.JPG", "B.JPG"]
        assert real.has_location and real.country == "Kenya"   # inherited, since the real day had none
        assert db.get(Session, wrong.id) is not None            # still holds the published photo
        with pytest.raises(ReviewError):
            shift_session_date(db, wrong, date(2013, 6, 16), WorkflowSettings())  # only published photos left


def test_lens_names_are_read_instead_of_codes(config, exiftool, exiftool_path, make_photo):
    photo = make_photo(config.dump_dir / "lens.JPG")
    subprocess.run([exiftool_path, "-q", "-overwrite_original", "-LensModel=", "-XMP:Lens=Sigma 70-300mm F4-5.6", str(photo)], check=True)

    meta = MetadataReader(exiftool).read_many([photo])[photo]

    assert meta.lens_model == "Sigma 70-300mm F4-5.6"
    assert is_lens_code("E0 3C 5C 8E 30 3C 4B 06") and not is_lens_code(meta.lens_model)


def test_the_worker_replaces_stored_lens_codes(scanner, clock, config, make_photo, database, monkeypatch):
    from aves_server.worker import Worker

    make_photo(config.dump_dir / "A.JPG")
    _ingest(scanner, clock)
    with database.session() as db:
        db.scalar(select(MediaFile)).lens_model = "E0 3C 5C 8E 30 3C 4B 06"   # as stored before the fix
    worker = Worker(config)
    try:
        assert worker._fix_lens_names() is True
        assert worker._fix_lens_names() is False
    finally:
        worker.exiftool.close()
    with database.session() as db:
        assert db.scalar(select(MediaFile)).lens_model == "NIKKOR Z 180-600mm"
