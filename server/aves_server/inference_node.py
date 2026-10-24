"""GPU inference node: lends a PC's graphics card to the workflow server over the network.

Runs the same Aves Caesar models as the server. On Windows it uses DirectML, which works with any
NVIDIA (or AMD/Intel) card through the normal graphics driver; no CUDA install is needed.

    python -m aves_server.inference_node --models <folder> --token <secret> [--port 8096]
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import secrets
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from PIL import Image

from bird_desktop.inference.pipeline import PipelineInput

from . import __version__
from .engine import LocalBackend, model_revision


MAX_BATCH = 16


def create_app(backend, token: str, models_dir: Path) -> FastAPI:
    app = FastAPI(title="Aves Caesar inference node", version=__version__)
    lock = threading.Lock()  # one batch at a time keeps GPU memory predictable

    def authorise(authorization: str | None) -> None:
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(supplied.encode(), token.encode()):
            raise HTTPException(401, "Invalid token")

    @app.get("/health")
    def health(authorization: str | None = Header(default=None)) -> dict:
        authorise(authorization)
        return {"ok": True, "version": __version__, "providers": backend.providers, "model_revision": model_revision(models_dir)}

    @app.post("/predict")
    def predict(meta: str = Form(...), images: list[UploadFile] = File(...), authorization: str | None = Header(default=None)) -> dict:
        authorise(authorization)
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
        finally:
            for item in inputs:
                item.image.close()
        return {"providers": backend.providers, "results": [{"provider": p.provider, "birds": p.birds} for p in predictions]}

    return app


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

    import uvicorn

    backend = LocalBackend(args.models.resolve(), args.provider)
    logging.getLogger(__name__).info("Models loaded: %s", backend.providers)
    uvicorn.run(create_app(backend, args.token, args.models.resolve()), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
