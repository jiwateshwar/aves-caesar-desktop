import copy
import io
import json
from types import SimpleNamespace

import pytest

from bird_desktop.collection.jobs import CollectionProcess
from bird_desktop.collection.transport import CollectionStatusDecoder, CollectionStatusEncoder
from bird_desktop.collection.worker import CollectionWorker
from bird_desktop.runtime.worker_io import WorkerStatusWriter


def snapshot(values, state="running"):
    return {"state": state, "completed": len(values), "species": [
        {"speciesId": "oiseau", "thumbnailDataUrl": values[0] if values else None,
         "occurrences": [{"imagePath": f"été-{i}.jpg", "birdIndex": i, "confidence": .50000001,
                          "thumbnailDataUrl": value, "thumbnailKey": str(i)} for i, value in enumerate(values)]}]}


def test_roundtrip_preserves_all_statuses_and_does_not_mutate_old_snapshots():
    encoder, decoder = CollectionStatusEncoder(), CollectionStatusDecoder()
    statuses = [snapshot([]), snapshot(["thumb-a", None]), snapshot(["thumb-a", None, "thumb-b", "thumb-a"], "done")]
    originals = copy.deepcopy(statuses)
    restored = []
    tables = []
    for status in statuses:
        payload = encoder.encode(status)
        tables.append(payload["_thumbnailValues"])
        restored.append(decoder.decode(json.loads(json.dumps(payload))))
    assert restored == originals == statuses
    assert tables == [[], ["thumb-a"], ["thumb-b"]]


def test_decoder_accepts_legacy_status_and_missing_thumbnails():
    decoder = CollectionStatusDecoder()
    original = {"state": "done", "species": [{"speciesId": "bird", "occurrences": [{}]}]}
    assert decoder.decode(copy.deepcopy(original)) == original
    assert decoder.decode(CollectionStatusEncoder().encode(original)) == original


@pytest.mark.parametrize("bad", [
    {"_collectionTransport": 2, "_thumbnailValues": []},
    {"_collectionTransport": 1, "_thumbnailValues": [5]},
    {"_collectionTransport": 1, "_thumbnailValues": [], "species": [{"thumbnailDataUrl": -1}]},
    {"_collectionTransport": 1, "_thumbnailValues": ["a"], "species": [{"thumbnailDataUrl": 1}]},
])
def test_decoder_rejects_invalid_reference_without_corrupting_table(bad):
    decoder = CollectionStatusDecoder()
    with pytest.raises(ValueError):
        decoder.decode(bad)
    assert decoder.decode(CollectionStatusEncoder().encode(snapshot(["valid"]))) == snapshot(["valid"])


def test_worker_and_reader_keep_public_status_identical(monkeypatch):
    monkeypatch.setattr("bird_desktop.collection.jobs.workers.stop", lambda _: None)
    stream = io.StringIO()
    worker = CollectionWorker()
    worker._status_writer = WorkerStatusWriter(stream)
    final = snapshot(["thumb-a", "thumb-a", "thumb-b"], "done")
    worker._write(snapshot(["thumb-a"]))
    worker._write(final)
    stream.seek(0)
    process = CollectionProcess(SimpleNamespace(stdout=stream, poll=lambda: 0))
    process._reader.join(timeout=2)
    assert not process._reader.is_alive()
    for key, value in final.items():
        assert process.status()[key] == value
    assert "_thumbnailValues" not in process.status()


def test_invalid_wire_status_reports_error_and_stops_child(monkeypatch):
    stopped = []
    monkeypatch.setattr("bird_desktop.collection.jobs.workers.stop", lambda _: stopped.append(True))
    line = json.dumps({"_collectionTransport": 9}) + "\n"
    process = CollectionProcess(SimpleNamespace(stdout=io.StringIO(line), poll=lambda: None,
                                                terminate=lambda: stopped.append(True)))
    process._reader.join(timeout=2)
    assert process.status()["state"] == "error"
    assert stopped == [True]
