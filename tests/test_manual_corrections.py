from pathlib import Path

from PIL import Image

from bird_desktop.collection.services import CollectionStore
from bird_desktop.data.bird_names import BirdNames
from bird_desktop.inference.cache import PredictionCache
from bird_desktop.inference.corrections import ManualCorrectionStore


class FakeNames:
    def __init__(self):
        self.names = {
            "amecro": BirdNames(name="American Crow", name_lat="Corvus brachyrhynchos"),
            "norcar": BirdNames(name="Northern Cardinal", name_lat="Cardinalis cardinalis"),
        }


    def all(self):
        return dict(self.names)


    def get(self, species_id: str):
        return self.names.get(species_id)


def test_manual_correction_promotes_corrected_species(tmp_path: Path) -> None:
    image_path = tmp_path / "birds.jpg"
    Image.new("RGB", (120, 90), "white").save(image_path)
    corrections = ManualCorrectionStore(tmp_path / "manual-corrections.sqlite3", FakeNames())
    corrections.set(image_path, 0, "norcar")

    result = corrections.apply(
        image_path,
        {
            "birds": [
                {
                    "box": [10, 10, 40, 50],
                    "classification": [
                        {"species_id": "amecro", "confidence": 0.81},
                        {"species_id": "norcar", "confidence": 0.22},
                    ],
                }
            ]
        },
    )

    bird = result["birds"][0]
    assert bird["classification"][0]["species_id"] == "norcar"
    assert bird["classification"][0]["manual"] is True
    assert bird["manualCorrection"]["originalClassification"]["species_id"] == "amecro"


def test_collection_store_counts_manual_correction_below_threshold(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    image_path = source / "birds.jpg"
    Image.new("RGB", (120, 90), "white").save(image_path)
    cache = PredictionCache(tmp_path / "prediction-cache.sqlite3")
    cache.store(image_path, "model-v1", "location-v1", {"birds": [{"box": [10, 10, 40, 50], "classification": [{"species_id": "amecro", "confidence": 0.42}]}]})
    corrections = ManualCorrectionStore(tmp_path / "manual-corrections.sqlite3", FakeNames())
    corrections.set(image_path, 0, "norcar")
    store = CollectionStore(tmp_path / "collection-index.sqlite3", cache, corrections)

    status = store.load(source, 0.9, "model-v1", 0.01)

    assert status is not None
    assert [item["speciesId"] for item in status["species"]] == ["norcar"]
    assert status["species"][0]["occurrences"][0]["classification"]["manual"] is True
    assert store.load(source, 0.9, "model-v1", 0.01) == status
    assert cache.lookup_for_model(image_path, "model-v1")["result"]["birds"][0]["classification"][0]["species_id"] == "amecro"


def test_bulk_corrections_reuse_paths_without_caching_file_state_or_manual_edits(tmp_path):
    image = tmp_path / "birds.jpg"
    image.write_bytes(b"original")
    store = ManualCorrectionStore(tmp_path / "corrections.sqlite3", FakeNames())
    result = {"birds": [{"box": [1, 2, 3, 4], "classification": [
        {"species_id": "amecro", "confidence": .8}, {"species_id": "norcar", "confidence": .2}]}]}
    keys = {image: str(image.resolve()).casefold()}
    store.set(image, 0, "norcar")
    assert store.apply_many([(image, result)], path_keys=keys) == [store.apply(image, result)]
    assert store.apply_many([(image, result)], path_keys={}) == [store.apply(image, result)]
    assert result["birds"][0]["classification"][0]["species_id"] == "amecro"
    image.write_bytes(b"new image of different size")
    assert store.apply_many([(image, result)], path_keys=keys) == [result]
    store.set(image, 0, "norcar")
    assert store.apply_many([(image, result)], path_keys=keys)[0]["birds"][0]["classification"][0]["manual"] is True
    store.clear(image, 0)
    assert store.apply_many([(image, result)], path_keys=keys) == [result]
    image.unlink()
    assert store.apply_many([(image, result)], path_keys=keys) == [result]


def test_bulk_corrections_default_keeps_nested_caller_data_isolated(tmp_path):
    image = tmp_path / "bird.jpg"
    image.write_bytes(b"photo")
    store = ManualCorrectionStore(tmp_path / "corrections.sqlite3", FakeNames())
    result = {"provider": ["CPU"], "birds": [{"box": [1, 2, 3, 4], "classification": [
        {"species_id": "amecro", "confidence": .8}]}]}
    store.set(image, 0, "norcar")
    corrected = store.apply_many([(image, result)])[0]
    corrected["provider"].append("changed")
    corrected["birds"][0]["box"][0] = 99
    assert result == {"provider": ["CPU"], "birds": [{"box": [1, 2, 3, 4], "classification": [
        {"species_id": "amecro", "confidence": .8}]}]}


def test_bulk_corrections_empty_store_does_not_inspect_unrelated_files(tmp_path, monkeypatch):
    store = ManualCorrectionStore(tmp_path / "corrections.sqlite3", FakeNames())
    path = tmp_path / "unneeded.jpg"
    result = {"birds": [{"classification": [{"species_id": "amecro", "confidence": .8}]}]}
    def unexpected(*args):
        raise AssertionError("An empty correction store must not inspect photo identities")
    monkeypatch.setattr(store, "_identity", unexpected)
    copied = store.apply_many([(path, result)])[0]
    assert copied == result and copied is not result
    assert copied["birds"][0] is not result["birds"][0]
    assert store.apply_many([(path, result)], copy_results=False)[0] is result


def test_bulk_corrections_only_stat_matching_paths_and_keep_order(tmp_path, monkeypatch):
    store = ManualCorrectionStore(tmp_path / "corrections.sqlite3", FakeNames())
    paths = [tmp_path / f"bird-{i}.jpg" for i in range(4)]
    for path in paths:
        path.write_bytes(b"photo")
    store.set(paths[1], 0, "norcar")
    store.set(paths[2], 0, "norcar")
    paths[2].unlink()
    result = {"birds": [{"classification": [{"species_id": "amecro", "confidence": .8}]}]}
    entries = [(path, result) for path in [paths[3], paths[1], paths[0], paths[2], paths[1]]]
    expected = [store.apply(path, result) for path, result in entries]
    identity = store._identity
    seen = []
    def track(path, key=None):
        seen.append(path)
        return identity(path, key)
    monkeypatch.setattr(store, "_identity", track)
    assert store.apply_many(entries, path_keys={paths[1]: str(paths[1].resolve()).casefold()}) == expected
    assert seen == [paths[1], paths[2], paths[1]]
    assert result["birds"][0]["classification"][0]["species_id"] == "amecro"
