import json
from pathlib import Path
import runpy
import sys

import pytest
from PIL import Image

from bird_desktop.collection.services import CollectionScanner
from bird_desktop.collection.worker import CollectionWorker
from bird_desktop.inference.cache import PredictionCache
from bird_desktop.inference.pipeline import BirdOnnxPipeline
from bird_desktop.inference.providers import ProviderSelection, ProviderSelector
from bird_desktop.media.exiftool import ExifTool
from bird_desktop.media.image_io import ImageLoader
from bird_desktop.runtime.config import RuntimeConfig
from bird_desktop.runtime.paths import AppPaths


BENCHMARK_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/dev/benchmark_collection.py"


@pytest.mark.skipif(
    not BENCHMARK_SCRIPT.is_file(),
    reason="The optional local benchmark script is not included in the repository.",
)
def test_benchmark_records_both_runs_and_cleans_isolated_cache(monkeypatch, tmp_path):
    image = tmp_path / "bird.jpg"
    Image.new("RGB", (12, 12)).save(image)
    output = tmp_path / "report.json"
    # Record attributes reassigned by the standalone harness for fixture cleanup.
    for owner, names in [
        (CollectionScanner, ["scan_images"]), (RuntimeConfig, ["load"]), (AppPaths, ["cache_dir"]),
        (ImageLoader, ["load", "read_metadata"]), (ExifTool, ["extract_preview"]),
        (BirdOnnxPipeline, ["__init__", "_run_detector", "_run_classifier"]),
    ]:
        for name in names:
            monkeypatch.setattr(owner, name, owner.__dict__[name])
    monkeypatch.setattr(ProviderSelector, "select", staticmethod(lambda _: ProviderSelection([], [], ["CPUExecutionProvider"])))
    caches = []
    def scan(worker, request, runtime):
        cache = PredictionCache()
        caches.append(cache.path)
        cache.store(image, "model", "location", {"birds": []})
        worker._write({"species": [], "errors": 0, "completed": 1})
    monkeypatch.setattr(CollectionWorker, "_run_collection", scan)
    monkeypatch.setattr(sys, "argv", ["benchmark", "--source", str(tmp_path), "--jpeg-only", "--output", str(output)])
    runpy.run_path(str(BENCHMARK_SCRIPT), run_name="benchmark_test")["main"]()
    report = json.loads(output.read_text())
    assert [run["kind"] for run in report["runs"]] == ["cold", "cached"]
    assert all(run["cpu_seconds"] >= 0 and run["seconds"] > 0 for run in report["runs"])
    assert all(0 <= run["first_result_seconds"] <= run["seconds"] for run in report["runs"])
    assert all(run["predictions"][0]["result"] == {"birds": []} for run in report["runs"])
    assert all(not cache.exists() for cache in caches)
