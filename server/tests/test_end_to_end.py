"""Runs the worker loop on real photos with the real models.

Needs the model files (AVES_MODELS_DIR, default ../models) and a folder of bird photos named after the
species they show, e.g. kingfisher.jpg (AVES_TEST_PHOTOS). Skipped otherwise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from aves_server.models import Shot, ShotStatus
from aves_server.review import confirm_shot
from aves_server.worker import Worker


MODELS_DIR = Path(os.environ.get("AVES_MODELS_DIR", Path(__file__).resolve().parents[2] / "models"))
PHOTOS_DIR = Path(os.environ["AVES_TEST_PHOTOS"]) if os.environ.get("AVES_TEST_PHOTOS") else None
EXPECTED = {"kingfisher.jpg": "Alcedo atthis", "heron.jpg": "Ardea cinerea"}

pytestmark = [
    pytest.mark.models,
    pytest.mark.skipif(not (MODELS_DIR / "bird_classifier.onnx").exists(), reason="model files not present"),
    pytest.mark.skipif(PHOTOS_DIR is None or not all((PHOTOS_DIR / name).exists() for name in EXPECTED), reason="AVES_TEST_PHOTOS not set"),
]


def test_worker_identifies_and_publishes_real_birds(config, exiftool_path, monkeypatch):
    monkeypatch.setenv("AVES_MODELS_DIR", str(MODELS_DIR))
    for name in EXPECTED:
        target = config.dump_dir / "card" / name.upper()
        target.parent.mkdir(exist_ok=True)
        shutil.copy2(PHOTOS_DIR / name, target)
        # Wimbledon Common, London: both species occur there.
        subprocess.run([exiftool_path, "-q", "-overwrite_original", "-DateTimeOriginal=2026:05:02 07:30:00",
                        "-GPSLatitude=51.4347", "-GPSLatitudeRef=N", "-GPSLongitude=0.2327", "-GPSLongitudeRef=W", str(target)], check=True)

    worker = Worker(config)
    try:
        worker.scanner.scan(quick=True)
        while worker.tick():
            pass

        with worker.database.session() as db:
            shots = list(db.scalars(select(Shot)))
            assert {s.status for s in shots} == {ShotStatus.review}
            found = {s.files[0].original_filename.lower(): s.detections[0].species.scientific_name for s in shots}
            assert found == EXPECTED
            assert all(worker.previews.path(s.id, "thumb").exists() for s in shots)
            for shot in shots:
                confirm_shot(shot)

        from aves_server.jobs import enqueue
        with worker.database.session() as db:
            enqueue(db, "publish", {})
        while worker.tick():
            pass
        published = sorted(p.relative_to(config.published_dir).as_posix() for p in config.published_dir.rglob("*.JPG"))
        assert len(published) == 2 and all(p.startswith("United Kingdom/England/") and "/2026-05-02/JPG/" in p for p in published)
    finally:
        worker.exiftool.close()
