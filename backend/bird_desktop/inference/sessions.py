from __future__ import annotations

import logging
from pathlib import Path
import time

from .onnxruntime_loader import OnnxRuntimeLoader

logger = logging.getLogger(__name__)


def stable_classifier_batch_size(session, config) -> int | None:
    """Only dynamic DirectML classifiers use stable input shapes, without graph overrides."""
    size = getattr(config, "directml_classifier_batch_size", 0)
    inputs = session.get_inputs()
    if (size > 0 and inputs and inputs[0].shape
            and not isinstance(inputs[0].shape[0], int)
            and "DmlExecutionProvider" in session.get_providers()):
        return min(size, max(1, config.classifier_batch_size))
    return None


def create_session(path: Path, providers: list[str], config=None, batch_size: int | None = None):
    """The probe and the prediction worker must use the same session options."""
    OnnxRuntimeLoader.prepare()
    import onnxruntime as ort

    options = ort.SessionOptions()
    if "DmlExecutionProvider" in providers:
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.enable_mem_pattern = False
    if getattr(config, "intra_op_threads", 0):
        options.intra_op_num_threads = config.intra_op_threads
    if not getattr(config, "intra_op_spinning", True):
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    profile_directory = getattr(config, "profile_directory", None)
    if profile_directory:
        Path(profile_directory).mkdir(parents=True, exist_ok=True)
        options.enable_profiling = True
        options.profile_file_prefix = str(Path(profile_directory) / path.stem)
    if getattr(config, "fixed_batch", False) and batch_size:
        options.add_free_dimension_override_by_name("batch", batch_size)
    start = time.perf_counter()
    try:
        return ort.InferenceSession(str(path), sess_options=options, providers=providers)
    except Exception:
        logger.exception("ONNX session failed: model=%s providers=%s", path.name, providers)
        raise
    finally:
        logger.info("ONNX initialization: model=%s seconds=%.3f", path.name, time.perf_counter() - start)
