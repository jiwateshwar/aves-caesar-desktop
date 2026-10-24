"""Builds dist/aves-inference-node.zip: everything a Windows PC needs to run the GPU inference node.

    python server/windows/build_node_bundle.py
"""

from __future__ import annotations

import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "dist" / "aves-inference-node.zip"
TOP = "aves-inference-node"

INCLUDE = [
    ("server/windows/start-inference-node.ps1", "start-inference-node.ps1"),
    ("server/windows/aves-node-tray.pyw", "aves-node-tray.pyw"),
    ("icon.png", "icon.png"),
    ("backend/bird_desktop", "backend/bird_desktop"),
    ("server/aves_server/__init__.py", "server/aves_server/__init__.py"),
    ("server/aves_server/engine.py", "server/aves_server/engine.py"),
    ("server/aves_server/inference_node.py", "server/aves_server/inference_node.py"),
    ("server/aves_server/tray.py", "server/aves_server/tray.py"),
    ("scripts/models/download_release_models.py", "scripts/models/download_release_models.py"),
    ("model-version.json", "model-version.json"),
    ("runtime_config.json", "runtime_config.json"),
    ("LICENSE.txt", "LICENSE.txt"),
    ("NOTICE.txt", "NOTICE.txt"),
    ("licenses", "licenses"),
]

README = """Aves Caesar GPU inference node
==============================

Lends this PC's graphics card to the Aves Caesar workflow server on your NAS.

1. Install the latest graphics driver and Python 3.11+ from https://www.python.org/downloads/windows/
   (tick "Add python.exe to PATH").
2. Keep this whole folder together, somewhere permanent (e.g. C:\\AvesNode). The models (1.8 GB) are
   downloaded into it on the first run.
3. Open PowerShell as Administrator (only the first time, to open the firewall port) and run:
       powershell -ExecutionPolicy Bypass -File C:\\AvesNode\\start-inference-node.ps1
4. Copy the AVES_REMOTE_URL (your home-network address) and AVES_REMOTE_TOKEN it prints into the
   NAS stack's environment variables and redeploy the stack.

It then runs in the system tray and starts by itself whenever you sign in to Windows. Right-click the
icon to pause it (frees the graphics card's memory; the NAS uses its own processor meanwhile), resume,
turn "Start with Windows" off, or quit. To update, unzip the new version over this folder and run
step 3 again; the models and token are kept.

Licensed under the GNU AGPL v3 or later; see LICENSE.txt.
"""


def files(source: Path):
    if source.is_file():
        yield source
        return
    for path in sorted(source.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            yield path


def main() -> None:
    OUTPUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as bundle:
        for source_name, target_name in INCLUDE:
            source = ROOT / source_name
            for path in files(source):
                relative = path.relative_to(source) if source.is_dir() else Path()
                bundle.write(path, f"{TOP}/{target_name}/{relative.as_posix()}".rstrip("/.") if source.is_dir() else f"{TOP}/{target_name}")
        bundle.writestr(f"{TOP}/README.txt", README.replace("\n", "\r\n"))
    print(f"Wrote {OUTPUT} ({OUTPUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
