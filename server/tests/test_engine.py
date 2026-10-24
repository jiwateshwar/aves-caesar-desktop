from __future__ import annotations

import json
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from bird_desktop.inference.pipeline import PipelineInput

from aves_server.analysis import Analyzer, BackendFailed
from aves_server.engine import FallbackBackend, Prediction, RemoteBackend, RemoteUnavailable
from aves_server.inference_node import create_app
from aves_server.models import Shot, ShotStatus
from aves_server.previews import PreviewStore


POISON = (255, 0, 0)


def _is_red(pixel) -> bool:  # JPEG compression shifts colours slightly
    red, green, blue = pixel[:3]
    return red > 200 and green < 60 and blue < 60


def _bird(species_id="101", confidence=0.9):
    return {"box": [1.0, 1.0, 20.0, 20.0], "box_confidence": 0.9, "classification": [{"species_id": species_id, "confidence": confidence}]}


class FakeBackend:
    """Stands in for the models. A pure red photo makes it throw, like a photo that breaks the model."""

    def __init__(self, broken: bool = False):
        self.broken = broken
        self.calls: list[int] = []

    @property
    def providers(self):
        return {"via": "local", "detector": ["DmlExecutionProvider"], "classifier": ["DmlExecutionProvider"]}

    def predict_many(self, inputs):
        self.calls.append(len(inputs))
        if self.broken or any(_is_red(item.image.getpixel((5, 5))) for item in inputs):
            raise ValueError("model exploded")
        return [Prediction(["DmlExecutionProvider"], [_bird()]) for _ in inputs]


def _remote(models_dir, node_backend, token="secret", node_revision="abc", client_token="secret") -> RemoteBackend:
    node_models = models_dir / "node"
    node_models.mkdir(exist_ok=True)
    (node_models / "model-build-info.json").write_text(json.dumps({"revision": node_revision}))
    remote = RemoteBackend("http://testserver", client_token, models_dir)
    remote._client = TestClient(create_app(node_backend, token, node_models), headers={"Authorization": f"Bearer {client_token}"})
    return remote


@pytest.fixture
def models_dir(tmp_path):
    folder = tmp_path / "models"
    folder.mkdir()
    (folder / "model-build-info.json").write_text(json.dumps({"revision": "abc"}))
    return folder


def test_photos_are_identified_on_the_inference_node(models_dir):
    remote = _remote(models_dir, FakeBackend())
    inputs = [PipelineInput(Image.new("RGB", (64, 48), (0, 90, 0)), 11.4, 76.7, "2026:10:18 07:00:00") for _ in range(2)]

    predictions = remote.predict_many(inputs)

    assert [p.birds[0]["classification"][0]["species_id"] for p in predictions] == ["101", "101"]
    assert remote.providers["via"] == "remote" and remote.providers["classifier"] == ["DmlExecutionProvider"]


def test_wrong_token_or_model_version_makes_the_node_unusable(models_dir):
    with pytest.raises(RemoteUnavailable, match="token"):
        _remote(models_dir, FakeBackend(), client_token="wrong").check()
    with pytest.raises(RemoteUnavailable, match="model revision"):
        _remote(models_dir, FakeBackend(), node_revision="other").check()


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class DownRemote:
    url = "http://pc:8096"
    providers = {"via": "remote", "node": url}

    def __init__(self):
        self.up = False

    def predict_many(self, inputs):
        if not self.up:
            raise RemoteUnavailable("Can't reach the inference node")
        return [Prediction(["CUDAExecutionProvider"], []) for _ in inputs]


def test_node_switched_off_falls_back_to_local_then_returns_to_the_node():
    clock, remote, local = FakeClock(), DownRemote(), FakeBackend()
    backend = FallbackBackend(remote, lambda: local, "local", clock)
    item = [PipelineInput(Image.new("RGB", (8, 8)), None, None, None)]

    assert backend.predict_many(item)[0].provider == ["DmlExecutionProvider"]   # ran locally
    assert backend.providers["via"] == "local" and backend.providers["remote_error"]
    remote.up = True
    assert backend.predict_many(item)[0].provider == ["DmlExecutionProvider"]   # still in the retry pause
    clock.now += 61
    assert backend.predict_many(item)[0].provider == ["CUDAExecutionProvider"]  # node is back


def test_wait_mode_holds_photos_until_the_node_is_back():
    clock, remote = FakeClock(), DownRemote()
    backend = FallbackBackend(remote, lambda: pytest.fail("must not load local models"), "wait", clock)
    with pytest.raises(RemoteUnavailable):
        backend.predict_many([PipelineInput(Image.new("RGB", (8, 8)), None, None, None)])
    assert backend.waiting()
    clock.now += 61
    assert not backend.waiting()


def _queue(scanner, clock, config, make_photo, colors):
    for minute, color in enumerate(colors):
        make_photo(config.dump_dir / f"P{minute}.JPG", gps=(11.41, 76.70), color=color, taken=datetime(2026, 10, 18, 7, minute))
    scanner.scan()
    clock.advance(31)
    scanner.scan()


def _analyzer(config, database, exiftool, backend):
    return Analyzer(config, database, PreviewStore(config.cache_dir), exiftool, backend=backend)


def test_a_photo_that_breaks_the_model_does_not_block_the_others(scanner, clock, config, make_photo, database, exiftool):
    from aves_server.catalog import SpeciesCatalog
    from pathlib import Path
    with database.session() as db:
        SpeciesCatalog(Path(__file__).parent / "data" / "species.csv").seed(db)
    _queue(scanner, clock, config, make_photo, [(0, 90, 0), POISON, (0, 0, 90)])
    analyzer = _analyzer(config, database, exiftool, FakeBackend())

    outcome = analyzer.analyse_pending()
    assert (outcome.analysed, outcome.failed) == (2, 1)
    for _ in range(2):  # retried, then given up on after 3 attempts
        analyzer.analyse_pending()

    with database.session() as db:
        statuses = {s.files[0].original_filename: s.status for s in db.scalars(select(Shot))}
        poison = db.scalar(select(Shot).where(Shot.status == ShotStatus.error))
        assert statuses == {"P0.JPG": ShotStatus.review, "P1.JPG": ShotStatus.error, "P2.JPG": ShotStatus.review}
        assert "model exploded" in poison.analysis_error and "3 attempts" in poison.analysis_error


def test_a_batch_where_everything_fails_is_reported_as_a_backend_failure(scanner, clock, config, make_photo, database, exiftool):
    _queue(scanner, clock, config, make_photo, [(0, 90, 0), (0, 0, 90)])
    analyzer = _analyzer(config, database, exiftool, FakeBackend(broken=True))

    with pytest.raises(BackendFailed):
        analyzer.analyse_pending()
    with database.session() as db:
        assert {(s.status, s.analysis_attempts) for s in db.scalars(select(Shot))} == {(ShotStatus.pending, 1)}


def test_waiting_for_the_node_does_not_count_as_a_failed_attempt(scanner, clock, config, make_photo, database, exiftool):
    _queue(scanner, clock, config, make_photo, [(0, 90, 0)])
    backend = FallbackBackend(DownRemote(), lambda: pytest.fail("no local models"), "wait", FakeClock())
    analyzer = _analyzer(config, database, exiftool, backend)

    with pytest.raises(RemoteUnavailable):
        analyzer.analyse_pending()
    with database.session() as db:
        assert db.scalar(select(Shot)).analysis_attempts == 0


def test_pausing_the_node_unloads_the_models_and_the_nas_treats_it_as_offline(models_dir):
    from aves_server.inference_node import NodeBackend

    loaded = []
    node = NodeBackend(models_dir, "directml", factory=lambda _models, _provider: loaded.append(1) or FakeBackend())
    node.start(wait=True)
    remote = _remote(models_dir, node)
    item = [PipelineInput(Image.new("RGB", (32, 32), (0, 90, 0)), None, None, None)]
    assert node.state == "running" and len(remote.predict_many(item)) == 1 and node.served == 1

    node.pause()
    assert node.state == "paused" and node._backend is None
    with pytest.raises(RemoteUnavailable, match="paused"):
        remote.predict_many(item)

    node.resume()
    for _ in range(100):
        if node.state == "running":
            break
        time.sleep(0.01)
    assert node.state == "running" and len(loaded) == 2
    assert len(remote.predict_many(item)) == 1


def test_models_that_fail_to_load_put_the_node_in_error(models_dir):
    from aves_server.inference_node import NodeBackend

    def broken(_models, _provider):
        raise RuntimeError("no DirectML device")

    node = NodeBackend(models_dir, "directml", factory=broken)
    node.start(wait=True)
    assert (node.state, node.error) == ("error", "no DirectML device")
