from pathlib import Path
from types import SimpleNamespace

import pytest

from bird_desktop.collection import worker as module


@pytest.mark.parametrize("cached_count", [0, 35000, 35010])
def test_worker_reports_only_new_processing_in_eta_metrics(monkeypatch, cached_count):
    clock = [0.0]
    images = [Path(f"{i}.jpg") for i in range(35010 if cached_count else 10)]
    keys = {path: str(path) for path in images}
    cached = {keys[path]: {} for path in images[:cached_count]}
    store = SimpleNamespace(image_keys=lambda _: keys, thumbnail_data_url=None,
                            save_thumbnail_records=lambda _: None)
    indexer = SimpleNamespace(species=lambda: [], _thumbnail_data_url=None,
                              add_prediction=lambda *args: [])
    monkeypatch.setattr(module, "CollectionStore", lambda: store)
    monkeypatch.setattr(module, "CollectionIndexer", lambda _: indexer)
    monkeypatch.setattr(module.CollectionScanner, "scan_images", lambda *_: images)
    monkeypatch.setattr(module.RuntimeConfig, "load", lambda: SimpleNamespace(min_classification_confidence=0))
    monkeypatch.setattr(module.PredictionCache, "model_fingerprint", lambda _: "model")
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    worker = module.CollectionWorker()
    statuses = []
    monkeypatch.setattr(worker, "_write", statuses.append)
    monkeypatch.setattr(worker, "_write_log", lambda _: None)
    monkeypatch.setattr(worker, "_cached_predictions_by_image", lambda *_: cached)
    monkeypatch.setattr(worker, "_prediction_service_for_runtime", lambda _: None)

    def reuse(*_):
        clock[0] = 500.0  # Slow cache loading must not count toward inference time.
        return cached_count

    def predictions(paths, *_):
        assert paths == images[cached_count:]
        for i, path in enumerate(paths):
            clock[0] += 2.0
            worker._event_thumbnails = None
            yield path, {} if i else None, None if i else ValueError("Unreadable image")

    monkeypatch.setattr(worker, "_reuse_cached_predictions", reuse)
    monkeypatch.setattr(worker, "_prediction_results_for_chunk", predictions)
    worker._run_collection({"baseDirectory": "photos"}, SimpleNamespace(selection=SimpleNamespace(selected=[])))
    assert statuses[0]["processed"] == 0
    assert statuses[0]["processingElapsedSeconds"] == 0
    for status in statuses:
        assert status["cached"] == cached_count
        assert status["processed"] == status["completed"] - cached_count
        assert status["processingElapsedSeconds"] == status["processed"] * 2
    assert statuses[-1]["state"] == "done"
    assert statuses[-1]["completed"] == len(images)
    assert statuses[-1]["errors"] == (1 if cached_count < len(images) else 0)
