from dataclasses import replace
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from bird_desktop.inference.preprocess import BirdBox, BirdImagePreprocessor
from bird_desktop.inference.services import PredictionService
from bird_desktop.inference.streaming import PredictionStream
from bird_desktop.media.exiftool import ExifTool
from bird_desktop.media.image_io import ImageLoader, ImageMetadata
from bird_desktop.runtime.config import RuntimeConfig
from bird_desktop.runtime.paths import AppPaths


class Pipeline:
    def __init__(self, bad_detector=None, bad_classifier=None):
        self.detector_batches = []
        self.classifier_batches = []
        self.bad_detector = bad_detector
        self.bad_classifier = bad_classifier

    def _detect_birds_many(self, images):
        values = [image.getpixel((0, 0))[0] for image in images]
        self.detector_batches.append(values)
        if self.bad_detector in values:
            raise ValueError("bad detector input")
        return [[BirdBox([1., 1., 10., 10.], .95)] for _ in images]

    def _run_classifier_batch(self, images, metadata):
        values = [int(image[0, 0, 0]) for image in images]
        self.classifier_batches.append(values)
        if self.bad_classifier in values:
            raise ValueError("bad classifier input")
        return [[{"species_id": str(value), "confidence": .9}] for value in values]

    def _active_providers(self):
        return ["CPUExecutionProvider"]


@pytest.fixture
def setup_stream(monkeypatch, tmp_path):
    monkeypatch.setattr(AppPaths, "cache_dir", staticmethod(lambda: tmp_path / "cache"))
    monkeypatch.setattr(ExifTool, "find", staticmethod(lambda: None))
    monkeypatch.setattr(ImageLoader, "read_metadata", staticmethod(lambda _: ImageMetadata(None, None, None)))
    monkeypatch.setattr(BirdImagePreprocessor, "_classifier_tensor_from_box", staticmethod(
        lambda job, size: np.full((3, size, size), job.image.getpixel((0, 0))[0], dtype=np.float32)))
    config = RuntimeConfig(tmp_path / "detector", tmp_path / "classifier", tmp_path / "labels", crop_size=8)
    def make(count=65, pipeline=None):
        paths = []
        for i in range(count):
            path = tmp_path / f"{i:03}.png"
            Image.new("RGB", (16, 16), (i, 0, 0)).save(path)
            paths.append(path)
        service = PredictionService(config)
        pipeline = pipeline or Pipeline()
        service._get_pipeline = lambda: pipeline
        requests = [{"imagePath": str(p), "includePreview": False} for p in paths]
        return service, pipeline, requests
    return make


def test_continuous_batches_cross_old_chunk_boundaries_and_preserve_order(setup_stream):
    service, pipeline, requests = setup_stream()
    service.config = replace(service.config, batch_wait_seconds=10)
    events = list(service.iter_prediction_events(requests, .5))
    assert [e.index for e in events] == list(range(65))
    assert [e.result["birds"][0]["classification"][0]["species_id"] for e in events] == list(map(str, range(65)))
    assert not any(e.error for e in events)
    assert max(map(len, pipeline.classifier_batches)) == 32
    assert sum(map(len, pipeline.classifier_batches)) == 65
    assert not [t for t in threading.enumerate() if t.name.startswith("bird-stream-")]


@pytest.mark.parametrize("stage", ["detector", "classifier"])
@pytest.mark.parametrize("directml", [False, True])
def test_failed_batch_is_split_and_later_images_still_use_batches(setup_stream, stage, directml):
    pipeline = Pipeline(**{f"bad_{stage}": 5})
    if directml:
        pipeline.detector_session = pipeline.classifier_session = _session("DmlExecutionProvider")
    service, pipeline, requests = setup_stream(40, pipeline)
    events = list(service.iter_prediction_events(requests, .5))
    assert [e.index for e in events if e.error] == [5]
    assert len([e for e in events if e.result]) == 39
    batches = getattr(pipeline, f"{stage}_batches")
    assert any(len(batch) > 1 and min(batch) > 5 for batch in batches)


def test_corrupt_file_does_not_abort_stream(setup_stream):
    service, _, requests = setup_stream(40)
    Path(requests[4]["imagePath"]).write_bytes(b"invalid image")
    events = list(service.iter_prediction_events(requests, .5))
    assert len(events) == 40
    assert [e.index for e in events if e.error] == [4]


@pytest.mark.parametrize("directml", [False, True])
def test_early_close_releases_images_and_joins_threads(setup_stream, monkeypatch, directml):
    service, pipeline, requests = setup_stream(80)
    if directml:
        pipeline.detector_session = pipeline.classifier_session = _session("DmlExecutionProvider")
    original_load, original_cleanup = ImageLoader.load, ImageLoader.cleanup
    active = set()
    maximum = [0]
    lock = threading.Lock()
    def load(path):
        loaded = original_load(path)
        with lock:
            active.add(id(loaded))
            maximum[0] = max(maximum[0], len(active))
        return loaded
    def cleanup(loaded):
        with lock:
            active.remove(id(loaded))
        original_cleanup(loaded)
    monkeypatch.setattr(ImageLoader, "load", staticmethod(load))
    monkeypatch.setattr(ImageLoader, "cleanup", staticmethod(cleanup))
    events = service.iter_prediction_events(requests, .5)
    next(events)
    events.close()
    assert active == set()
    assert maximum[0] <= 8
    assert not [t for t in threading.enumerate() if t.name.startswith(("bird-stream-", "bird-load"))]


def test_thumbnails_use_loaded_image_and_survive_result_filter(setup_stream, monkeypatch):
    from bird_desktop.collection.services import CollectionIndexer
    service, _, requests = setup_stream(3)
    indexer = CollectionIndexer()
    events = list(service.iter_prediction_events(requests, .5, thumbnail_factory=indexer._thumbnail_data_url))
    monkeypatch.setattr(ImageLoader, "load", staticmethod(lambda _: pytest.fail("image decoded twice")))
    for event in events:
        records = indexer.add_prediction(event.image_path, event.result, .5, event.thumbnails)
        assert records[0]["thumbnailDataUrl"].startswith("data:image/jpeg;base64,")


def test_cache_only_stream_never_loads_models_or_images(setup_stream, monkeypatch):
    service, _, requests = setup_stream(4)
    first = list(service.iter_prediction_events(requests, .5))
    service._get_pipeline = lambda: pytest.fail("model loaded on cache hit")
    monkeypatch.setattr(ImageLoader, "load", staticmethod(lambda _: pytest.fail("image loaded on cache hit")))
    cached = list(service.iter_prediction_events(requests, .5))
    assert all(e.result["source"] == "cache" for e in cached)
    assert [e.result["birds"] for e in cached] == [e.result["birds"] for e in first]


def test_no_bird_images_finish_without_waiting_for_classifier(setup_stream):
    service, pipeline, requests = setup_stream(70)
    pipeline._detect_birds_many = lambda images: [[] for _ in images]
    events = list(service.iter_prediction_events(requests, .5))
    assert len(events) == 70
    assert all(e.result["birds"] == [] for e in events)
    assert pipeline.classifier_batches == []


def test_crop_failure_waits_for_other_readers_before_closing_image(setup_stream, monkeypatch):
    service, pipeline, requests = setup_stream(1)
    pipeline._detect_birds_many = lambda images: [[BirdBox([float(i), 1., 10., 10.], .9) for i in range(3)] for _ in images]
    original = BirdImagePreprocessor._classifier_tensor_from_box
    reading = set()
    lock = threading.Lock()
    def crop(job, size):
        index = job.box.box[0]
        if index == 0:
            raise ValueError("bad crop")
        with lock:
            reading.add(index)
        try:
            time.sleep(.03)
            return original(job, size)
        finally:
            with lock:
                reading.remove(index)
    cleanup = ImageLoader.cleanup
    def close(loaded):
        assert not reading
        cleanup(loaded)
    monkeypatch.setattr(BirdImagePreprocessor, "_classifier_tensor_from_box", staticmethod(crop))
    monkeypatch.setattr(ImageLoader, "cleanup", staticmethod(close))
    events = list(service.iter_prediction_events(requests, .5))
    assert len(events) == 1
    assert isinstance(events[0].error, ValueError)


def test_invalid_request_is_an_individual_error(setup_stream):
    service, _, requests = setup_stream(4)
    requests[1] = {"includePreview": False}
    events = list(service.iter_prediction_events(requests, .5))
    assert [e.index for e in events if e.error] == [1]
    assert len(events) == 4


@pytest.mark.parametrize("count", [1, 3, 4, 7])
def test_detector_startup_deadline_fills_batch_and_preserves_end_marker(tmp_path, count):
    config = RuntimeConfig(tmp_path / "d", tmp_path / "c", tmp_path / "l", batch_wait_seconds=.001)
    service = SimpleNamespace(config=config)
    stream = PredictionStream(service, [], .5, "model")
    stream.detect_queue.put(0)
    def produce():
        # Initial decoding exceeds the steady-state deadline, but not startup's.
        time.sleep(.05)
        for index in range(1, count):
            stream.detect_queue.put(index)
        stream.detect_queue.put(stream.sentinel)
    producer = threading.Thread(target=produce)
    producer.start()
    service._get_pipeline = lambda: object()
    batches = []
    stream._detect_batch = lambda batch: batches.append(list(batch))
    stream._detect()
    producer.join(timeout=2)
    assert not producer.is_alive()
    assert batches == [list(range(start, min(start + 4, count))) for start in range(0, count, 4)]
    assert stream.detect_queue.empty()
    assert [stream.crop_queue.get_nowait() for _ in range(stream.workers)] == [stream.sentinel] * stream.workers


def test_startup_wait_does_not_shorten_a_longer_configured_wait(tmp_path):
    config = RuntimeConfig(tmp_path / "d", tmp_path / "c", tmp_path / "l")
    stream = PredictionStream(SimpleNamespace(config=config), [], .5, "model")
    assert stream._initial_detector_batch_wait() == 1.0
    stream.config = replace(config, batch_wait_seconds=2.0)
    assert stream._initial_detector_batch_wait() == 2.0


def _session(provider, axis="batch"):
    return SimpleNamespace(get_providers=lambda: [provider],
                           get_inputs=lambda: [SimpleNamespace(shape=[axis, 3, 8, 8])])


@pytest.mark.parametrize("provider,axis,stable,expected", [
    ("DmlExecutionProvider", "batch", 4, (4, 2.0, 1.0)),
    ("DmlExecutionProvider", "batch", 0, (32, .1, 1.0)),
    ("DmlExecutionProvider", 8, 4, (32, .1, .1)),
    ("CPUExecutionProvider", "batch", 4, (32, .1, .1)),
    ("CoreMLExecutionProvider", "batch", 4, (32, .1, .1)),
])
def test_stream_grouping_uses_effective_session_and_preserves_other_backends(
    tmp_path, provider, axis, stable, expected
):
    config = RuntimeConfig(tmp_path / "d", tmp_path / "c", tmp_path / "l",
                           directml_classifier_batch_size=stable)
    stream = PredictionStream(SimpleNamespace(config=config), [], .5, "model")
    # Merely querying grouping policy must not initialize the pipeline.
    assert stream._classifier_batch_size() == 32
    assert stream._classifier_batch_wait() == .1
    assert stream._detector_batch_wait() == .1
    stream.pipeline = SimpleNamespace(detector_session=_session(provider, axis),
                                      classifier_session=_session(provider, axis))
    assert (stream._classifier_batch_size(), stream._classifier_batch_wait(),
            stream._detector_batch_wait()) == expected
    stream.config = replace(config, classifier_batch_wait_seconds=.25)
    assert stream._classifier_batch_wait() == .25
    stream.config = replace(config, batch_wait_seconds=3)
    assert stream._classifier_batch_wait() == 3
    assert stream._detector_batch_wait() == 3


def test_directml_stream_fills_effective_batches_preserving_order_and_bounds(setup_stream):
    service, pipeline, requests = setup_stream(70)
    pipeline.detector_session = pipeline.classifier_session = _session("DmlExecutionProvider")
    events = list(service.iter_prediction_events(requests, .5))
    assert [e.index for e in events] == list(range(70))
    assert [e.result["birds"][0]["classification"][0]["species_id"] for e in events] == list(map(str, range(70)))
    assert not any(e.error for e in events)
    assert all(len(batch) == 4 for batch in pipeline.classifier_batches[1:-1])
    assert max(map(len, pipeline.classifier_batches)) == 4
    assert sum(map(len, pipeline.classifier_batches)) == 70
    assert not [t for t in threading.enumerate() if t.name.startswith(("bird-stream-", "bird-load"))]


def test_classifier_partial_batch_deadline_and_eof_with_lazy_limits(tmp_path):
    config = RuntimeConfig(tmp_path / "d", tmp_path / "c", tmp_path / "l", batch_wait_seconds=.01)
    stream = PredictionStream(SimpleNamespace(config=config), [], .5, "model")
    stream.classify_queue.put(0)
    batches = stream._batches(stream.classify_queue, lambda: 4, followup_wait=lambda: .02)
    assert next(batches) == [0]  # Partial first result, no need for a full batch.
    stream.classify_queue.put(1)
    assert next(batches) == [1]  # Sparse birds also flush on the follow-up deadline.
    stream.classify_queue.put(2)
    stream.classify_queue.put(stream.sentinel)
    assert list(batches) == [[2]]


def test_cancel_interrupts_long_batch_wait(tmp_path):
    config = RuntimeConfig(tmp_path / "d", tmp_path / "c", tmp_path / "l", batch_wait_seconds=30)
    stream = PredictionStream(SimpleNamespace(config=config), [], .5, "model")
    stream.classify_queue.put(0)
    finished = threading.Event()
    def consume():
        list(stream._batches(stream.classify_queue, lambda: 4, followup_wait=lambda: 30))
        finished.set()
    consumer = threading.Thread(target=consume)
    consumer.start()
    stream.cancelled.set()
    consumer.join(timeout=2)
    assert finished.is_set()
    assert not consumer.is_alive()
