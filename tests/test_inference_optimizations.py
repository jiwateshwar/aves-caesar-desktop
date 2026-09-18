from types import SimpleNamespace
import sys
import threading

import numpy as np
import pytest
from PIL import Image

from bird_desktop.inference.pipeline import BirdOnnxPipeline
from bird_desktop.inference.metrics import InferenceMetrics
from bird_desktop.inference.preprocess import BirdImagePreprocessor, BirdBox, ClassifierCropJob, IMAGENET_MEAN, IMAGENET_STD


def test_planar_classifier_normalization_is_exact_for_every_byte_value():
    values = np.arange(256, dtype=np.uint8).reshape(16, 16)
    pixels = np.stack([values, np.flipud(values), np.fliplr(values)], axis=-1)
    with Image.fromarray(pixels) as image:
        actual = BirdImagePreprocessor._classifier_tensor(image, 16)
        expected = pixels.astype(np.float32)
        expected /= 255.0
        expected -= IMAGENET_MEAN
        expected /= IMAGENET_STD
        np.testing.assert_array_equal(actual, expected.transpose(2, 0, 1))
        assert actual.dtype == np.float32
        assert actual.flags.c_contiguous
        assert image.tobytes() == pixels.tobytes()


@pytest.mark.parametrize("box,area,paste", [
    ([15, 15, 25, 25], (10, 10, 30, 30), (0, 0)),
    ([0, 0, 10, 10], (0, 0, 15, 15), (5, 5)),
    ([35, 35, 45, 45], (30, 30, 40, 40), (0, 0)),
])
def test_crop_fast_path_preserves_pixels_padding_and_float32_normalization(box, area, paste):
    rng = np.random.default_rng(19)
    image = Image.fromarray(rng.integers(0, 256, (40, 40, 3), dtype=np.uint8))
    expected = Image.new("RGB", (20, 20), (114, 114, 114))
    with image.crop(area) as patch:
        expected.paste(patch, paste)
    item = BirdBox(box, .9)
    actual = BirdImagePreprocessor._square_crop(image, item, 40, 40)
    try:
        assert actual.tobytes() == expected.tobytes()
        with expected.resize((224, 224), Image.Resampling.BICUBIC) as resized:
            values = np.asarray(resized, dtype=np.float32) / 255.0
            values = (values - IMAGENET_MEAN) / IMAGENET_STD
        np.testing.assert_array_equal(
            BirdImagePreprocessor._classifier_tensor_from_box(ClassifierCropJob(image, item), 224),
            values.transpose(2, 0, 1))
    finally:
        actual.close()
        expected.close()
        image.close()
from bird_desktop.inference.sessions import create_session
from bird_desktop.runtime.config import RuntimeConfig


def test_contiguous_detector_batch_preserves_original_float32_values():
    rng = np.random.default_rng(12)
    images = [Image.fromarray(rng.integers(0, 255, (35, 71, 3), dtype=np.uint8)) for _ in range(4)]
    batch, scales = BirdImagePreprocessor.detector_batch(images)
    for index, image in enumerate(images):
        resized = image.resize((640, 640), Image.Resampling.BICUBIC)
        expected = np.transpose(np.asarray(resized, dtype=np.float32) / 255., (2, 0, 1)).astype(np.float32)
        np.testing.assert_array_equal(batch[index], expected)
        assert scales[index] == (71 / 640, 35 / 640)
    assert batch.flags.c_contiguous
    assert batch.dtype == np.float32


def test_session_factory_uses_directml_constraints_and_optional_fixed_batch(monkeypatch, tmp_path):
    options = SimpleNamespace(add_free_dimension_override_by_name=lambda name, size: overrides.append((name, size)))
    overrides = []
    seen = {}
    def session(path, providers, sess_options):
        seen.update(path=path, providers=providers, options=sess_options)
        return seen
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        SessionOptions=lambda: options, ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"), InferenceSession=session))
    monkeypatch.setattr("bird_desktop.inference.sessions.OnnxRuntimeLoader.prepare", lambda: None)
    config = RuntimeConfig(tmp_path / "d", tmp_path / "c", tmp_path / "l", fixed_batch=True, intra_op_threads=2)
    create_session(config.detector_model, ["DmlExecutionProvider", "CPUExecutionProvider"], config, 4)
    assert options.enable_mem_pattern is False
    assert options.execution_mode == "sequential"
    assert options.intra_op_num_threads == 2
    assert overrides == [("batch", 4)]


def test_static_batch_padding_keeps_all_inputs_aligned():
    session = SimpleNamespace(get_inputs=lambda: [SimpleNamespace(shape=[4, 3, 2, 2])])
    feeds = {"img": np.ones((2, 3, 2, 2), np.float32), "lat": np.array([1., 2.], np.float32)}
    padded = BirdOnnxPipeline._padded_feeds(session, feeds)
    assert padded["img"].shape[0] == 4
    np.testing.assert_array_equal(padded["lat"], [1, 2, 2, 2])
    assert feeds["lat"].shape == (2,)


def test_detector_filter_keeps_python_float_threshold_semantics():
    pipeline = SimpleNamespace(config=SimpleNamespace(detection_threshold=.7), detector_labels={"14": "bird"})
    scores = np.array([.7, .700001, .9, .5], dtype=np.float32)
    boxes = np.array([[1, 2, 3, 4]] * 4, dtype=np.float32)
    labels = np.array([14, 14, 13, 14])
    result = BirdOnnxPipeline._postprocess_detector_rows(pipeline, boxes, scores, labels, (1., 1.))
    assert len(result) == 1
    assert result[0].score == float(scores[1])


@pytest.mark.parametrize("provider,shape,bucket,maximum,expected_sizes", [
    ("DmlExecutionProvider", "batch", 4, 32, [4, 4]),
    ("DmlExecutionProvider", "batch", 4, 2, [2, 2, 2]),
    ("DmlExecutionProvider", "batch", 0, 32, [6]),
    ("CPUExecutionProvider", "batch", 4, 32, [6]),
    ("CoreMLExecutionProvider", "batch", 4, 32, [6]),
    ("DmlExecutionProvider", 8, 4, 32, [8]),
])
def test_stable_classifier_calls_keep_metadata_order_and_discard_padding(
    tmp_path, provider, shape, bucket, maximum, expected_sizes
):
    pipeline = object.__new__(BirdOnnxPipeline)
    pipeline.config = RuntimeConfig(tmp_path / "d", tmp_path / "c", tmp_path / "l",
                                    classifier_batch_size=maximum, directml_classifier_batch_size=bucket)
    pipeline.class_ids = []
    pipeline._classifier_lock = threading.Lock()
    pipeline.metrics = InferenceMetrics()
    seen = []
    def run(_outputs, feeds):
        seen.append({key: value.copy() for key, value in feeds.items()})
        ids = feeds["img"][:, 0, 0, 0].astype(np.int64)
        np.testing.assert_array_equal(feeds["lat"], ids + 10)
        np.testing.assert_array_equal(feeds["lon"], ids + 20)
        assert np.isnan(feeds["day"]).all()
        return [np.full((len(ids), 1), .75, dtype=np.float32), ids[:, None]]
    pipeline.classifier_session = SimpleNamespace(
        get_inputs=lambda: [SimpleNamespace(shape=[shape, 3, 2, 2])],
        get_providers=lambda: [provider], run=run)
    images = np.stack([np.full((3, 2, 2), i, np.float32) for i in range(6)])
    original = images.copy()
    metadata = [(i + 10, i + 20, None) for i in range(6)]
    results = pipeline._run_classifier_batch(images, metadata)
    assert results == [[{"species_id": str(i), "confidence": .75}] for i in range(6)]
    assert [len(feeds["img"]) for feeds in seen] == expected_sizes
    np.testing.assert_array_equal(images, original)
    assert metadata == [(i + 10, i + 20, None) for i in range(6)]
