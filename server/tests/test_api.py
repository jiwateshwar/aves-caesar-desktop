from __future__ import annotations

import base64
import dataclasses
import shutil
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from aves_server.analysis import Analyzer
from aves_server.api import app, state
from aves_server.models import Job, Shot, ShotStatus
from aves_server.previews import PreviewStore
from aves_server.publisher import LibraryIndexer, Publisher


SPECIES_CSV = Path(__file__).parent / "data" / "species.csv"


@pytest.fixture
def client(config, monkeypatch):
    models = config.data_dir / "models"
    models.mkdir()
    shutil.copy(SPECIES_CSV, models / "species_mapping_v2.csv")
    for key, value in {"AVES_DATA_DIR": config.data_dir, "AVES_DATABASE_URL": config.database_url, "AVES_DUMP_DIR": config.dump_dir,
                       "AVES_PUBLISHED_DIR": config.published_dir, "AVES_MODELS_DIR": models}.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv("AVES_PASSWORD", raising=False)
    state.catalog = None
    with TestClient(app) as client:
        from aves_server.catalog import SpeciesCatalog
        with state.database.session() as db:
            SpeciesCatalog.load(models / "species_mapping_v2.csv").seed(db)
        yield client


def _dump_and_analyse(scanner, clock, config, make_photo, database, photos: dict[str, dict]):
    for minute, (name, spec) in enumerate(photos.items()):
        make_photo(config.dump_dir / spec.get("folder", "card") / name, gps=spec.get("gps"), taken=datetime(2026, 10, 18, 7, minute))
    scanner.scan()
    clock.advance(31)
    scanner.scan()
    with database.session() as db:
        for shot in db.scalars(select(Shot).where(Shot.status == ShotStatus.pending)):
            birds = photos[shot.files[0].original_filename].get("birds", [])
            Analyzer._store(shot, birds, (96, 64), True, ["CPUExecutionProvider"])
            PreviewStore(config.cache_dir).save(shot.id, Image.new("RGB", (96, 64)))
            shot.preview_ready = True


def _bird(species_id, confidence):
    return {"box": [5, 5, 40, 30], "box_confidence": 0.9, "classification": [{"species_id": species_id, "confidence": confidence},
                                                                              {"species_id": "103", "confidence": 0.02}]}


def test_review_and_publish_through_the_api(client, scanner, clock, config, make_photo, database):
    _dump_and_analyse(scanner, clock, config, make_photo, database, {
        "sure.JPG": {"gps": (11.41, 76.70), "birds": [_bird("101", 0.96)]},
        "unsure.JPG": {"gps": (11.41, 76.70), "birds": [_bird("102", 0.31)]},
        "empty.JPG": {"gps": (11.41, 76.70)},
        "nogps.JPG": {"folder": "phone"},
    })

    sessions = client.get("/api/sessions").json()
    assert len(sessions) == 1
    counts = sessions[0]["counts"]
    assert (counts["review"], counts["low_confidence"], counts["no_bird"], counts["awaiting_location"]) == (1, 1, 1, 1)
    session_id = sessions[0]["id"]
    detail = client.get(f"/api/sessions/{session_id}").json()
    assert [f["folder"] for f in detail["folders"]] == ["card", "phone"]

    low = client.get("/api/shots", params={"session_id": session_id, "category": "low_confidence"}).json()["items"]
    assert [s["species"][0]["name"] for s in low] == ["Common Kingfisher"]
    shot = client.get(f"/api/shots/{low[0]['id']}").json()
    assert shot["detections"][0]["alternatives"][1]["name"] == "Grey Heron"
    assert shot["files"][0]["publish_to"] == "India/Tamil Nadu/Ooty/2026-10-18/JPG/unsure.JPG"
    assert client.get(f"/api/shots/{shot['id']}/thumb.jpg").headers["content-type"] == "image/jpeg"

    detection_id = shot["detections"][0]["id"]
    shot = client.post(f"/api/shots/{shot['id']}/detections/{detection_id}/species", json={"species_id": "103"}).json()
    assert shot["species"][0]["name"] == "Grey Heron"
    assert client.post(f"/api/shots/{shot['id']}/confirm").json()["status"] == "confirmed"

    assert client.post(f"/api/sessions/{session_id}/confirm-confident").json() == {"confirmed": 1}
    empty = client.get("/api/shots", params={"category": "no_bird"}).json()["items"][0]
    assert client.post(f"/api/shots/{empty['id']}/ignore").json()["status"] == "ignored"

    found = client.get("/api/locations/search", params={"q": "ooty"}).json()[0]
    updated = client.patch(f"/api/sessions/{session_id}", json={"location": {"latitude": found["latitude"], "longitude": found["longitude"]}}).json()
    assert updated["queued"] == 1 and updated["location"]["city"] == "Ooty"

    job = client.post("/api/publish", json={"session_id": session_id}).json()
    assert job["status"] == "queued"
    with database.session() as db:
        shot_ids = [s.id for s in db.scalars(select(Shot).where(Shot.status == ShotStatus.confirmed))]
    assert Publisher(config, database).publish(shot_ids).published_files == 2
    assert client.get(f"/api/sessions/{session_id}").json()["counts"]["published"] == 2
    assert client.get("/api/status").json()["totals"]["ignored"] == 1


def test_conflicting_name_is_listed_and_resolved_with_a_suffix(client, scanner, clock, config, make_photo, database):
    (config.published_dir / "old").mkdir()
    (config.published_dir / "old" / "DSC_0009.JPG").write_bytes(b"x")
    LibraryIndexer(config, database).reindex()
    _dump_and_analyse(scanner, clock, config, make_photo, database, {"DSC_0009.JPG": {"gps": (11.41, 76.70), "birds": [_bird("101", 0.9)]}})

    conflicts = client.get("/api/shots", params={"category": "conflicts"}).json()["items"]
    assert len(conflicts) == 1 and conflicts[0]["files"][0]["conflict"] == "library"
    bad = client.post(f"/api/shots/{conflicts[0]['id']}/suffix", json={"suffix": "a/b"})
    assert bad.status_code == 409
    renamed = client.post(f"/api/shots/{conflicts[0]['id']}/suffix", json={"suffix": "_2"}).json()
    assert renamed["files"][0]["final_filename"] == "DSC_0009_2.JPG" and renamed["has_conflict"] is False


def test_duplicate_copy_can_be_deleted_from_the_dump(client, scanner, clock, config, make_photo, database):
    original = make_photo(config.dump_dir / "a" / "X.JPG")
    (config.dump_dir / "b").mkdir()
    shutil.copy2(original, config.dump_dir / "b" / "X.JPG")
    scanner.scan()
    clock.advance(31)
    scanner.scan()

    duplicates = client.get("/api/duplicates").json()
    assert len(duplicates) == 1
    assert client.delete(f"/api/duplicates/{duplicates[0]['id']}").status_code == 200
    assert original.exists() and not (config.dump_dir / duplicates[0]["dump_relpath"]).exists()
    assert client.get("/api/duplicates").json() == []


def test_manual_scan_queues_once(client, database):
    first = client.post("/api/scan").json()
    second = client.post("/api/scan").json()
    assert first["id"] == second["id"]
    with database.session() as db:
        assert db.query(Job).count() == 1


def test_settings_round_trip_and_validation(client):
    assert client.put("/api/settings", json={"confidence_threshold": 0.7}).json()["values"]["confidence_threshold"] == 0.7
    assert client.put("/api/settings", json={"publish_pattern": "{nope}"}).status_code == 400


def test_password_protects_everything_but_health(client, monkeypatch):
    monkeypatch.setattr(state, "config", dataclasses.replace(state.config, password="s3cret"))
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/sessions").status_code == 401
    token = base64.b64encode(b"aves:s3cret").decode()
    assert client.get("/api/sessions", headers={"Authorization": f"Basic {token}"}).status_code == 200
