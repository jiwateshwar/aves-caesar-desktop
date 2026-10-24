"""Where the bird models run: on this machine, or on a GPU inference node elsewhere on the network.

Kept free of database imports so the Windows inference node only needs the engine and FastAPI.
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from bird_desktop.inference.pipeline import BirdOnnxPipeline, PipelineInput
from bird_desktop.runtime.config import RuntimeConfig


logger = logging.getLogger(__name__)

REMOTE_RETRY_SECONDS = 60


def load_runtime_config(models_dir: Path, provider: str | None = None) -> RuntimeConfig:
    """The engine's own tuning, with the models folder and provider taken from the environment."""
    os.environ.setdefault("AVES_MODELS_DIR", str(models_dir))
    runtime = RuntimeConfig.load(Path(os.environ["AVES_RUNTIME_CONFIG"]) if os.environ.get("AVES_RUNTIME_CONFIG") else None)
    return replace(runtime, provider_preference=provider or os.environ.get("AVES_PROVIDER", "auto"))


def prepare_gpu_libraries() -> None:
    # onnxruntime-gpu installed with its [cuda,cudnn] extras ships CUDA as pip wheels; load them before sessions.
    import onnxruntime

    preload = getattr(onnxruntime, "preload_dlls", None)
    if preload is not None and "CUDAExecutionProvider" in onnxruntime.get_available_providers():
        try:
            preload()
        except Exception:
            logger.exception("Could not preload CUDA libraries; GPU inference may fall back to CPU")


def model_revision(models_dir: Path) -> str | None:
    try:
        return json.loads((models_dir / "model-build-info.json").read_text(encoding="utf-8")).get("revision")
    except (OSError, ValueError):
        return None


@dataclass
class Prediction:
    provider: list[str]
    birds: list[dict[str, Any]]


class RemoteUnavailable(RuntimeError):
    """The inference node can't be used right now (switched off, unreachable, misconfigured)."""


class LocalBackend:
    def __init__(self, models_dir: Path, provider: str | None = None):
        prepare_gpu_libraries()
        self.runtime_config = load_runtime_config(models_dir, provider)
        self.pipeline = BirdOnnxPipeline(self.runtime_config)

    @property
    def providers(self) -> dict[str, Any]:
        runtime = self.pipeline.runtime
        return {"via": "local", "detector": runtime.detector_selection.selected, "classifier": runtime.classifier_selection.selected}

    def predict_many(self, inputs: list[PipelineInput]) -> list[Prediction]:
        return [Prediction(result.provider, result.birds) for result in self.pipeline.predict_many(inputs)]


class RemoteBackend:
    """Sends decoded photos to an inference node (see inference_node.py) and returns its detections."""

    def __init__(self, url: str, token: str, models_dir: Path, timeout: float = 600):
        import httpx

        self.url = url.rstrip("/")
        self.expected_revision = model_revision(models_dir)
        self._client = httpx.Client(timeout=httpx.Timeout(timeout, connect=10), headers={"Authorization": f"Bearer {token}"})
        self.node_providers: dict[str, Any] = {}
        self._checked = False

    @property
    def providers(self) -> dict[str, Any]:
        return {**self.node_providers, "via": "remote", "node": self.url}

    def check(self) -> None:
        health = self._request("GET", "/health")
        revision = health.get("model_revision")
        if self.expected_revision and revision and revision != self.expected_revision:
            raise RemoteUnavailable(f"The inference node has model revision {revision[:10]}, this server has {self.expected_revision[:10]}. "
                                    "Run the model download script on the node so species IDs match.")
        self.node_providers = health.get("providers", {})
        self._checked = True

    def predict_many(self, inputs: list[PipelineInput]) -> list[Prediction]:
        if not self._checked:
            self.check()
        files, meta = [], []
        for index, item in enumerate(inputs):
            buffer = io.BytesIO()
            # Full resolution: boxes come back in these pixel coordinates, and small birds need the detail.
            item.image.save(buffer, "JPEG", quality=95)
            files.append(("images", (f"{index}.jpg", buffer.getvalue(), "image/jpeg")))
            meta.append({"latitude": item.latitude, "longitude": item.longitude, "datetime": item.datetime_text})
        data = self._request("POST", "/predict", files=files, data={"meta": json.dumps(meta)})
        results = data.get("results", [])
        if len(results) != len(inputs):
            raise RemoteUnavailable("The inference node returned an incomplete answer")
        self.node_providers = data.get("providers", self.node_providers)
        return [Prediction(r.get("provider", []), r.get("birds", [])) for r in results]

    def _request(self, method: str, path: str, **kwargs) -> dict:
        import httpx

        try:
            response = self._client.request(method, self.url + path, **kwargs)
        except httpx.HTTPError as exc:
            self._checked = False
            raise RemoteUnavailable(f"Can't reach the inference node at {self.url}: {exc}") from exc
        if response.status_code == 401:
            raise RemoteUnavailable("The inference node rejected the token (check AVES_REMOTE_TOKEN)")
        if response.status_code >= 500:
            self._checked = False
            raise RemoteUnavailable(f"The inference node failed: {response.status_code} {response.text[:200]}")
        if response.status_code >= 400:
            # A 4xx other than auth means this photo was refused; let the caller treat it as a photo error.
            raise RuntimeError(f"The inference node refused the request: {response.status_code} {response.text[:200]}")
        return response.json()


class FallbackBackend:
    """Prefers the inference node; when it's off, either runs locally or waits for it to come back."""

    def __init__(self, remote: RemoteBackend, local_factory: Callable[[], LocalBackend], fallback: str = "local",
                 clock: Callable[[], float] = time.monotonic):
        if fallback not in ("local", "wait"):
            raise ValueError("AVES_REMOTE_FALLBACK must be 'local' or 'wait'")
        self.remote = remote
        self.local_factory = local_factory
        self.fallback = fallback
        self.clock = clock
        self._local: LocalBackend | None = None
        self._remote_down_until = 0.0
        self.remote_error: str | None = None

    @property
    def providers(self) -> dict[str, Any]:
        if self._remote_down_until <= self.clock() or self._local is None:
            return {**self.remote.providers, "remote_error": self.remote_error}
        return {**self._local.providers, "node": self.remote.url, "remote_error": self.remote_error}

    def waiting(self) -> bool:
        """True while in 'wait' mode and the node is known to be down: no point loading photos yet."""
        return self.fallback == "wait" and self._remote_down_until > self.clock()

    def predict_many(self, inputs: list[PipelineInput]) -> list[Prediction]:
        if self._remote_down_until <= self.clock():
            try:
                predictions = self.remote.predict_many(inputs)
                self.remote_error = None
                return predictions
            except RemoteUnavailable as exc:
                logger.warning("Inference node unavailable: %s", exc)
                self.remote_error = str(exc)
                self._remote_down_until = self.clock() + REMOTE_RETRY_SECONDS
        if self.fallback == "wait":
            raise RemoteUnavailable(self.remote_error or "Waiting for the inference node")
        if self._local is None:
            logger.info("Loading the models on this machine while the inference node is unavailable")
            self._local = self.local_factory()
        return self._local.predict_many(inputs)


def backend_from_env(models_dir: Path):
    url = os.environ.get("AVES_REMOTE_URL", "").strip()
    if not url:
        return LocalBackend(models_dir)
    token = os.environ.get("AVES_REMOTE_TOKEN", "")
    if not token:
        raise RuntimeError("AVES_REMOTE_URL is set but AVES_REMOTE_TOKEN is empty")
    remote = RemoteBackend(url, token, models_dir)
    return FallbackBackend(remote, lambda: LocalBackend(models_dir), os.environ.get("AVES_REMOTE_FALLBACK", "local"))
