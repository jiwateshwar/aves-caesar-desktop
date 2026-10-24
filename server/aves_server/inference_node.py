"""GPU inference node: lends a PC's graphics card to the workflow server over the network.

Runs the same Aves Caesar models as the server. On Windows it uses DirectML, which works with any
NVIDIA (or AMD/Intel) card through the normal graphics driver; no CUDA install is needed.

    python -m aves_server.inference_node --models <folder> --token <secret> [--port 8096]

On Windows the tray app (tray.py) runs this in the background with pause/resume.
"""

from __future__ import annotations

import argparse
import gc
import io
import json
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from PIL import Image

from bird_desktop.inference.pipeline import PipelineInput

from . import __version__
from .engine import LocalBackend, model_revision


MAX_BATCH = 16
logger = logging.getLogger(__name__)


class NodeUnavailable(RuntimeError):
    pass


class NodeBackend:
    """The models, loaded in the background and unloadable on pause so the GPU's memory is freed."""

    def __init__(self, models_dir: Path, provider: str, factory: Callable[[Path, str], object] = LocalBackend):
        self.models_dir = models_dir
        self.provider = provider
        self.factory = factory
        self.state = "stopped"   # stopped | loading | running | paused | error
        self.error: str | None = None
        self.served = 0
        self.on_change: Callable[[], None] = lambda: None
        self._backend = None
        self._lock = threading.RLock()

    def start(self, wait: bool = False) -> None:
        with self._lock:
            if self.state in ("loading", "running"):
                return
            self._set("loading")
        thread = threading.Thread(target=self._load, name="aves-model-loader", daemon=True)
        thread.start()
        if wait:
            thread.join()

    def _load(self) -> None:
        try:
            backend = self.factory(self.models_dir, self.provider)
        except Exception as exc:
            logger.exception("Could not load the models")
            with self._lock:
                self.error = str(exc)
                self._set("error")
            return
        with self._lock:
            if self.state != "loading":  # paused while loading
                del backend
                gc.collect()
                return
            self._backend, self.error = backend, None
            self._set("running")
            logger.info("Models loaded: %s", backend.providers)

    def pause(self) -> None:
        with self._lock:
            self._backend = None
            self._set("paused")
        gc.collect()  # drops the ONNX sessions, which releases GPU memory

    def resume(self) -> None:
        self.start()

    @property
    def providers(self) -> dict:
        backend = self._backend
        return backend.providers if backend is not None else {"via": "local"}

    def predict_many(self, inputs):
        with self._lock:
            backend = self._backend
            if self.state != "running" or backend is None:
                raise NodeUnavailable(f"The inference node is {self.state}")
            predictions = backend.predict_many(inputs)
            self.served += len(inputs)
        self.on_change()
        return predictions

    def _set(self, state: str) -> None:
        self.state = state
        logger.info("Node %s", state)
        try:
            self.on_change()
        except Exception:
            logger.exception("Status update failed")


def create_app(backend, token: str, models_dir: Path) -> FastAPI:
    app = FastAPI(title="Aves Caesar inference node", version=__version__)
    lock = threading.Lock()  # one batch at a time keeps GPU memory predictable

    def authorise(authorization: str | None) -> None:
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(401, "Invalid token")

    def require_running() -> None:
        # 503 makes the NAS treat the node like a switched-off PC: it falls back or waits, then retries.
        state = getattr(backend, "state", "running")
        if state != "running":
            raise HTTPException(503, f"The inference node is {state}")

    @app.get("/health")
    def health(authorization: str | None = Header(default=None)) -> dict:
        authorise(authorization)
        require_running()
        return {"ok": True, "version": __version__, "providers": backend.providers, "model_revision": model_revision(models_dir)}

    @app.post("/predict")
    def predict(meta: str = Form(...), images: list[UploadFile] = File(...), authorization: str | None = Header(default=None)) -> dict:
        authorise(authorization)
        require_running()
        try:
            items = json.loads(meta)
        except ValueError:
            raise HTTPException(400, "meta must be JSON")
        if not isinstance(items, list) or len(items) != len(images) or not 0 < len(images) <= MAX_BATCH:
            raise HTTPException(400, f"Send 1–{MAX_BATCH} images with one meta entry each")
        inputs = []
        try:
            for upload, item in zip(images, items):
                image = Image.open(io.BytesIO(upload.file.read()))
                inputs.append(PipelineInput(image.convert("RGB"), item.get("latitude"), item.get("longitude"), item.get("datetime")))
        except Exception as exc:
            raise HTTPException(400, f"Could not decode an image: {exc}")
        try:
            with lock:
                predictions = backend.predict_many(inputs)
        except NodeUnavailable as exc:
            raise HTTPException(503, str(exc))
        finally:
            for item in inputs:
                item.image.close()
        return {"providers": backend.providers, "results": [{"provider": p.provider, "birds": p.birds} for p in predictions]}

    return app


def make_server(backend, token: str, models_dir: Path, host: str, port: int):
    """A uvicorn server that can run on a background thread and be stopped with server.should_exit."""
    import uvicorn

    config = uvicorn.Config(create_app(backend, token, models_dir), host=host, port=port, log_config=None, access_log=False)
    return uvicorn.Server(config)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lend this machine's GPU to an Aves Caesar workflow server.")
    parser.add_argument("--models", type=Path, default=Path(os.environ.get("AVES_MODELS_DIR", "models")))
    parser.add_argument("--token", default=os.environ.get("AVES_NODE_TOKEN"), help="Shared secret; the server's AVES_REMOTE_TOKEN")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8096)
    parser.add_argument("--provider", default=os.environ.get("AVES_PROVIDER", "auto"), help="auto, directml, cuda or cpu")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.token:
        parser.error("a token is required (--token or AVES_NODE_TOKEN); use the same value as AVES_REMOTE_TOKEN on the server")

    models = args.models.resolve()
    backend = NodeBackend(models, args.provider)
    backend.start(wait=True)
    if backend.state != "running":
        raise SystemExit(f"Could not load the models: {backend.error}")
    make_server(backend, args.token, models, args.host, args.port).run()


if __name__ == "__main__":
    main()
