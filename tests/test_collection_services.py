from __future__ import annotations

import sqlite3
from pathlib import Path

from PIL import Image

from bird_desktop.collection.services import CollectionIndexer, CollectionScanner, CollectionStore
from bird_desktop.collection.worker import CollectionWorker
from bird_desktop.inference.cache import PredictionCache


def test_collection_scan_is_recursive(tmp_path: Path) -> None:
    source = tmp_path / "source"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (source / "root.jpg").write_text("x", encoding="utf-8")
    (nested / "nested.jpg").write_text("x", encoding="utf-8")

    assert CollectionScanner.scan_images(source) == [nested / "nested.jpg", source / "root.jpg"]


def test_collection_scan_ignores_hidden_files_and_directories(tmp_path: Path) -> None:
    source = tmp_path / "source"
    hidden_directory = source / ".hidden"
    nested = source / "nested"
    hidden_directory.mkdir(parents=True)
    nested.mkdir()
    (hidden_directory / "ignored.jpg").write_text("x", encoding="utf-8")
    (source / ".ignored.NEF").write_text("x", encoding="utf-8")
    used = nested / "used.jpg"
    used.write_text("x", encoding="utf-8")

    assert CollectionScanner.scan_images(source) == [used]


def test_collection_scan_ignores_hidden_source_directory(tmp_path: Path) -> None:
    source = tmp_path / ".source"
    source.mkdir()
    (source / "ignored.jpg").write_text("x", encoding="utf-8")

    assert CollectionScanner.scan_images(source) == []


def test_collection_scan_filters_by_scan_mode(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    raw = source / "bird.NEF"
    heic = source / "bird.HEIC"
    heif = source / "bird.heif"
    jpeg = source / "bird.jpg"
    png = source / "bird.png"
    raw.write_text("x", encoding="utf-8")
    heic.write_text("x", encoding="utf-8")
    heif.write_text("x", encoding="utf-8")
    jpeg.write_text("x", encoding="utf-8")
    png.write_text("x", encoding="utf-8")

    assert CollectionScanner.scan_images(source, "raw") == [raw]
    assert CollectionScanner.scan_images(source, "jpeg") == [heic, heif, jpeg]
    assert CollectionScanner.scan_images(source, "raw_jpeg") == [heic, heif, jpeg, raw]


def test_collection_indexer_groups_species_and_preserves_occurrences(tmp_path: Path) -> None:
    image_path = tmp_path / "birds.jpg"
    Image.new("RGB", (120, 90), "white").save(image_path)
    indexer = CollectionIndexer()

    indexer.add_prediction(
        image_path,
        {
            "birds": [
                {"box": [10, 10, 40, 50], "classification": [{"species_id": "amecro", "confidence": 0.91}]},
                {"box": [60, 20, 95, 70], "classification": [{"species_id": "amecro", "confidence": 0.86}]},
                {"box": [5, 5, 20, 25], "classification": [{"species_id": "norcar", "confidence": 0.74}]},
            ]
        },
        0.5,
    )

    species = {item["speciesId"]: item for item in indexer.species()}

    assert species["amecro"]["occurrenceCount"] == 2
    assert species["amecro"]["imageCount"] == 1
    assert species["norcar"]["occurrenceCount"] == 1


def test_collection_indexer_skips_low_confidence(tmp_path: Path) -> None:
    image_path = tmp_path / "birds.jpg"
    Image.new("RGB", (120, 90), "white").save(image_path)
    indexer = CollectionIndexer()

    indexer.add_prediction(image_path, {"birds": [{"box": [10, 10, 40, 50], "classification": [{"species_id": "amecro", "confidence": 0.49}]}]}, 0.5)

    assert indexer.species() == []


def test_collection_thumbnail_is_centered_data_url(tmp_path: Path) -> None:
    image_path = tmp_path / "birds.jpg"
    Image.new("RGB", (120, 90), "white").save(image_path)
    indexer = CollectionIndexer()

    indexer.add_prediction(image_path, {"birds": [{"box": [10, 10, 40, 50], "classification": [{"species_id": "amecro", "confidence": 0.91}]}]}, 0.5)

    species = indexer.species()

    assert species[0]["thumbnailDataUrl"].startswith("data:image/jpeg;base64,")
    assert species[0]["occurrences"][0]["thumbnailDataUrl"].startswith("data:image/jpeg;base64,")


def test_collection_indexer_reuses_cached_thumbnail_without_loading_image(tmp_path: Path) -> None:
    image_path = tmp_path / "birds.jpg"
    image_path.write_text("not an image", encoding="utf-8")
    indexer = CollectionIndexer(lambda _key: "data:image/jpeg;base64,cached")

    indexer.add_prediction(image_path, {"birds": [{"box": [10, 10, 40, 50], "classification": [{"species_id": "amecro", "confidence": 0.91}]}]}, 0.5)

    species = indexer.species()

    assert species[0]["thumbnailDataUrl"] == "data:image/jpeg;base64,cached"
    assert species[0]["occurrences"][0]["thumbnailDataUrl"] == "data:image/jpeg;base64,cached"


def test_collection_store_loads_cached_predictions_by_directory_prefix_and_threshold(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    image_path = source / "birds.jpg"
    Image.new("RGB", (120, 90), "white").save(image_path)
    outside_path = tmp_path / "source-other.jpg"
    Image.new("RGB", (120, 90), "white").save(outside_path)
    cache = PredictionCache(tmp_path / "prediction-cache.sqlite3")
    cache.store(
        image_path,
        "model-v1",
        "location-v1",
        {
            "birds": [
                {"box": [10, 10, 40, 50], "classification": [{"species_id": "amecro", "confidence": 0.91}]},
                {"box": [60, 20, 95, 70], "classification": [{"species_id": "norcar", "confidence": 0.61}]},
            ]
        },
    )
    cache.store(outside_path, "model-v1", "location-v1", {"birds": [{"box": [10, 10, 40, 50], "classification": [{"species_id": "outside", "confidence": 0.99}]}]})
    store = CollectionStore(tmp_path / "collection-index.sqlite3", cache)

    loaded = store.load(source, 0.7, "model-v1", 0.0)

    assert loaded is not None
    assert loaded["total"] == 1
    assert loaded["completed"] == 1
    assert [item["speciesId"] for item in loaded["species"]] == ["amecro"]


def test_collection_store_loads_lower_threshold_from_same_prediction_cache(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    image_path = source / "birds.jpg"
    Image.new("RGB", (120, 90), "white").save(image_path)
    cache = PredictionCache(tmp_path / "prediction-cache.sqlite3")
    cache.store(
        image_path,
        "model-v1",
        "location-v1",
        {
            "birds": [
                {"box": [10, 10, 40, 50], "classification": [{"species_id": "amecro", "confidence": 0.91}]},
                {"box": [60, 20, 95, 70], "classification": [{"species_id": "norcar", "confidence": 0.61}]},
            ]
        },
    )
    store = CollectionStore(tmp_path / "collection-index.sqlite3", cache)

    loaded = store.load(source, 0.5, "model-v1", 0.0)
    assert loaded is not None
    assert [item["speciesId"] for item in loaded["species"]] == ["amecro", "norcar"]


def test_collection_store_load_does_not_generate_missing_thumbnails(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    image_path = source / "birds.jpg"
    image_path.write_text("not an image", encoding="utf-8")
    cache = PredictionCache(tmp_path / "prediction-cache.sqlite3")
    cache.store(image_path, "model-v1", "location-v1", {"birds": [{"box": [10, 10, 40, 50], "classification": [{"species_id": "amecro", "confidence": 0.91}]}]})
    store = CollectionStore(tmp_path / "collection-index.sqlite3", cache)

    loaded = store.load(source, 0.5, "model-v1", 0.0)

    assert loaded is not None
    assert loaded["species"][0]["thumbnailDataUrl"] is None
    assert loaded["species"][0]["occurrences"][0]["thumbnailDataUrl"] is None


def test_collection_store_saves_thumbnails_without_collection_index_tables(tmp_path: Path) -> None:
    image_path = tmp_path / "birds.jpg"
    Image.new("RGB", (120, 90), "white").save(image_path)
    indexer = CollectionIndexer()
    indexer.add_prediction(image_path, {"birds": [{"box": [10, 10, 40, 50], "classification": [{"species_id": "amecro", "confidence": 0.91}]}]}, 0.5)
    store_path = tmp_path / "collection-index.sqlite3"
    store = CollectionStore(store_path)

    store.save_thumbnails({"species": indexer.species()})

    with sqlite3.connect(store_path) as connection:
        thumbnail_count = connection.execute("select count(*) from collection_thumbnails").fetchone()[0]
        collection_index_table = connection.execute("select name from sqlite_master where type = 'table' and name = 'collection_index'").fetchone()

    assert thumbnail_count == 1
    assert collection_index_table is None


def test_collection_worker_reuses_cached_predictions_before_processing_missing_images(tmp_path: Path) -> None:
    first_image = tmp_path / "first.jpg"
    second_image = tmp_path / "second.jpg"
    Image.new("RGB", (120, 90), "white").save(first_image)
    Image.new("RGB", (120, 90), "white").save(second_image)
    store = CollectionStore(tmp_path / "collection-index.sqlite3")
    indexer = CollectionIndexer()
    cached_predictions = {
        store.image_key(first_image): {
            "birds": [{"box": [10, 10, 40, 50], "classification": [{"species_id": "amecro", "confidence": 0.91}]}]
        }
    }
    processed_images: list[Path] = []

    completed = CollectionWorker()._reuse_cached_predictions([first_image, second_image], cached_predictions, 0.5, store, indexer, processed_images)

    assert completed == 1
    assert processed_images == [first_image]
    assert indexer.species()[0]["speciesId"] == "amecro"


def test_collection_worker_filters_cached_predictions_with_threshold(tmp_path: Path) -> None:
    image_path = tmp_path / "birds.jpg"
    Image.new("RGB", (120, 90), "white").save(image_path)
    store = CollectionStore(tmp_path / "collection-index.sqlite3")
    indexer = CollectionIndexer()
    cached_predictions = {
        store.image_key(image_path): {
            "birds": [
                {"box": [10, 10, 40, 50], "classification": [{"species_id": "amecro", "confidence": 0.91}]},
                {"box": [50, 10, 80, 50], "classification": [{"species_id": "norcar", "confidence": 0.61}]},
            ]
        }
    }
    processed_images: list[Path] = []

    completed = CollectionWorker()._reuse_cached_predictions([image_path], cached_predictions, 0.7, store, indexer, processed_images)
    species = indexer.species()

    assert completed == 1
    assert processed_images == [image_path]
    assert [item["speciesId"] for item in species] == ["amecro"]


def test_collection_store_keeps_scan_modes_separate(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    raw_path = source / "birds.NEF"
    jpeg_path = source / "birds.jpg"
    raw_path.write_text("raw", encoding="utf-8")
    Image.new("RGB", (120, 90), "white").save(jpeg_path)
    cache = PredictionCache(tmp_path / "prediction-cache.sqlite3")
    cache.store(jpeg_path, "model-v1", "location-v1", {"birds": [{"box": [10, 10, 40, 50], "classification": [{"species_id": "amecro", "confidence": 0.91}]}]})
    cache.store(raw_path, "model-v1", "location-v1", {"birds": [{"box": [10, 10, 40, 50], "classification": [{"species_id": "rawbird", "confidence": 0.91}]}]})
    store = CollectionStore(tmp_path / "collection-index.sqlite3", cache)

    assert store.load(source, 0.5, "model-v1", 0.0, "jpeg") is not None
    assert store.load(source, 0.5, "model-v1", 0.0, "jpeg")["species"][0]["speciesId"] == "amecro"
    assert store.load(source, 0.5, "model-v1", 0.0, "raw")["species"][0]["speciesId"] == "rawbird"


def test_thumbnail_records_are_written_only_once(tmp_path: Path, monkeypatch) -> None:
    store = CollectionStore(tmp_path / "collection.sqlite3")
    writes = []
    original = store._save_thumbnail
    def save(connection, occurrence, now):
        writes.append(occurrence["thumbnailKey"])
        original(connection, occurrence, now)
    monkeypatch.setattr(store, "_save_thumbnail", save)
    for index in range(10):
        record = {"thumbnailKey": str(index), "thumbnailDataUrl": "data:image/jpeg;base64,x"}
        store.save_thumbnail_records([record])
        store.save_thumbnail_records([record])
    assert writes == list(map(str, range(10)))
    store.prefetch_thumbnails(list(map(str, range(10))))
    assert store.thumbnail_data_url("9") == "data:image/jpeg;base64,x"


def test_bulk_thumbnail_keys_match_original_identity_and_refresh_after_changes(tmp_path: Path) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"first")
    indexer = CollectionIndexer()
    boxes = [[0., 1., 20., 30.], [5., 6., 25., 36.]]
    keys = indexer.thumbnail_keys(image, boxes)
    assert keys == [indexer._thumbnail_key(image, box) for box in boxes]
    image.write_bytes(b"changed-size")
    assert indexer.thumbnail_keys(image, boxes) != keys
    assert indexer.thumbnail_keys(tmp_path / "missing.jpg", []) == []


def test_cached_indexing_prepares_occurrences_and_thumbnail_keys_once(tmp_path, monkeypatch):
    path = tmp_path / "birds.jpg"
    Image.new("RGB", (40, 40)).save(path)
    result = {"birds": [
        {"box": [1, 1, 15, 15], "classification": [{"species_id": "first", "confidence": .9}]},
        {"box": [2, 2, 20, 20], "classification": [{"species_id": "second", "confidence": .2, "manual": True}]},
        {"box": [3, 3, 30, 30], "classification": [{"species_id": "excluded", "confidence": .1}]},
    ]}
    store = CollectionStore(tmp_path / "collection.sqlite3")
    reference = CollectionIndexer()
    records = reference.add_prediction(path, result, .5)
    store.save_thumbnail_records(records)
    indexer = CollectionIndexer(store.thumbnail_data_url)
    calls = []
    original = indexer.thumbnail_keys
    def keys(image_path, boxes, **kwargs):
        calls.append(image_path)
        return original(image_path, boxes, **kwargs)
    monkeypatch.setattr(indexer, "thumbnail_keys", keys)
    image_keys = {path: store.image_key(path)}
    processed = []
    count = CollectionWorker()._reuse_cached_predictions(
        [path], {image_keys[path]: result}, .5, store, indexer, processed, image_keys)
    assert count == 1 and processed == [path]
    assert calls == [path]
    assert indexer.species() == reference.species()
    assert [s["occurrences"][0]["birdIndex"] for s in indexer.species()] == [0, 1]


def test_supplied_thumbnail_path_key_still_checks_current_file_identity(tmp_path):
    path = tmp_path / "image.jpg"
    path.write_bytes(b"old")
    key = str(path.resolve()).casefold()
    indexer = CollectionIndexer()
    boxes = [[1., 2., 3., 4.]]
    before = indexer.thumbnail_keys(path, boxes, path_key=key)
    assert before == indexer.thumbnail_keys(path, boxes)
    path.write_bytes(b"new file size")
    after = indexer.thumbnail_keys(path, boxes, path_key=key)
    assert after != before
    assert after == indexer.thumbnail_keys(path, boxes)


def test_collection_load_refreshes_thumbnail_misses_written_by_another_worker(tmp_path: Path) -> None:
    source = tmp_path / "photos"
    source.mkdir()
    path = source / "bird.jpg"
    Image.new("RGB", (40, 40)).save(path)
    result = {"birds": [{"box": [1, 1, 20, 20], "classification": [{"species_id": "bird", "confidence": .9}]}]}
    cache = PredictionCache(tmp_path / "prediction-cache.sqlite3")
    cache.store(path, "model", "location", result)
    reader = CollectionStore(tmp_path / "collection.sqlite3", cache)
    assert reader.load(source, .5, "model", .01)["species"][0]["thumbnailDataUrl"] is None
    writer = CollectionStore(reader.path, cache)
    records = CollectionIndexer().add_prediction(path, result, .5)
    writer.save_thumbnail_records(records)
    assert reader.load(source, .5, "model", .01)["species"][0]["thumbnailDataUrl"].startswith("data:image/jpeg;base64,")
