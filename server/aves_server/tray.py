"""System tray app for the GPU inference node (Windows).

Runs the node in the background, shows its state in the tray, and lets you pause it (which unloads the
models and frees the graphics card's memory), resume it, toggle "Start with Windows", or quit.
Launched by aves-node-tray.pyw; set up by start-inference-node.ps1.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
from pathlib import Path

from PIL import Image, ImageDraw

from . import brand
from .inference_node import NodeBackend, make_server


APP_NAME = "FeatherbaseNode"
LEGACY_APP_NAMES = ("AvesCaesarNode",)  # earlier name; removed when autostart is set
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STATE_COLOURS = {"running": (46, 204, 113), "loading": (241, 196, 15), "paused": (149, 165, 166),
                 "error": (231, 76, 60), "stopped": (149, 165, 166)}
STATE_LABELS = {"running": "Running", "loading": "Loading models…", "paused": "Paused (GPU memory freed)",
                "error": "Error", "stopped": "Stopped"}

logger = logging.getLogger("aves_server.tray")


class Autostart:
    """Start with Windows: a per-user Run entry, so it starts at sign-in without admin rights."""

    def __init__(self, launcher: Path):
        self.launcher = launcher

    @property
    def command(self) -> str:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        return f'"{pythonw if pythonw.exists() else sys.executable}" "{self.launcher}"'

    def enabled(self) -> bool:
        try:
            import winreg
        except ImportError:  # not Windows
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _kind = winreg.QueryValueEx(key, APP_NAME)
                return bool(value)
        except OSError:
            return False

    def set(self, enabled: bool) -> None:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, self.command)
            for name in LEGACY_APP_NAMES + (() if enabled else (APP_NAME,)):
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass


def status_icon(base: Image.Image, state: str) -> Image.Image:
    """The app icon with a coloured status dot in the corner."""
    image = base.copy().convert("RGBA").resize((64, 64))
    draw = ImageDraw.Draw(image)
    draw.ellipse((38, 38, 63, 63), fill=STATE_COLOURS.get(state, (149, 165, 166)), outline=(255, 255, 255), width=3)
    return image


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


def message_box(text: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, brand.NODE_NAME, 0x40)
    except Exception:
        logger.warning(text)


class NodeTray:
    def __init__(self, root: Path, launcher: Path):
        import pystray

        self.pystray = pystray
        self.root = root
        settings = self._settings()
        self.port = int(settings.get("port", 8096))
        self.host = settings.get("host", "0.0.0.0")
        self.models = root / "models"
        self.token = (root / "node-token.txt").read_text(encoding="utf-8").strip()
        self.autostart = Autostart(launcher)
        self.backend = NodeBackend(self.models, settings.get("provider", "directml"))
        self.server = make_server(self.backend, self.token, self.models, self.host, self.port)
        icon_path = root / "icon.png"
        self.base_image = Image.open(icon_path) if icon_path.exists() else Image.new("RGBA", (64, 64), (34, 94, 60, 255))
        menu = pystray.Menu(
            pystray.MenuItem(lambda _item: self._status_line(), None, enabled=False),
            pystray.MenuItem(lambda _item: "Resume" if self.backend.state in ("paused", "error") else "Pause",
                             self.toggle_pause, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start with Windows", self.toggle_autostart, checked=lambda _item: self.autostart.enabled()),
            pystray.MenuItem("Open folder", lambda: os.startfile(self.root)),
            pystray.MenuItem("Open log", lambda: os.startfile(self.root / "node.log")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.quit),
        )
        self.icon = pystray.Icon(APP_NAME, status_icon(self.base_image, "loading"), brand.NODE_NAME, menu)

    def _settings(self) -> dict:
        try:
            return json.loads((self.root / "node-config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def run(self) -> None:
        if port_in_use(self.port):
            message_box(f"The {brand.NODE_NAME} is already running (port {self.port} is in use).")
            return
        threading.Thread(target=self.server.run, name="aves-node-server", daemon=True).start()
        self.backend.on_change = self.refresh
        self.backend.start()
        self.icon.run(setup=lambda icon: setattr(icon, "visible", True))

    def _status_line(self) -> str:
        state = self.backend.state
        label = STATE_LABELS.get(state, state)
        if state == "running":
            gpu = next((p for p in self.backend.providers.get("classifier", []) if p != "CPUExecutionProvider"), "CPU")
            label += f" · {gpu.replace('ExecutionProvider', '').replace('Dml', 'DirectML')} · {self.backend.served} photos"
        elif state == "error" and self.backend.error:
            label += f": {self.backend.error[:60]}"
        return label

    def refresh(self) -> None:
        # Called from background threads. Windows' tray API accepts that; macOS AppKit must be on the main thread.
        if sys.platform == "darwin":
            from PyObjCTools.AppHelper import callAfter

            callAfter(self._refresh_now)
        else:
            self._refresh_now()

    def _refresh_now(self) -> None:
        self.icon.icon = status_icon(self.base_image, self.backend.state)
        self.icon.title = f"{brand.NODE_NAME} — {self._status_line()}"[:127]
        self.icon.update_menu()

    def toggle_pause(self) -> None:
        if self.backend.state in ("paused", "error"):
            self.backend.resume()
        else:
            # Pausing waits for the photo batch in progress, then unloads the models.
            threading.Thread(target=self.backend.pause, daemon=True).start()

    def toggle_autostart(self) -> None:
        self.autostart.set(not self.autostart.enabled())
        self.icon.update_menu()

    def quit(self) -> None:
        logger.info("Quitting")
        self.server.should_exit = True
        self.backend.pause()
        self.icon.stop()


def configure_logging(root: Path) -> None:
    # pythonw.exe has no console; send everything (including uvicorn) to a rotating log file.
    handler = logging.handlers.RotatingFileHandler(root / "node.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, "w"))


def main(root: Path, launcher: Path) -> None:
    configure_logging(root)
    try:
        NodeTray(root, launcher).run()
    except Exception as exc:
        logger.exception("The tray app stopped")
        message_box(f"The {brand.NODE_NAME} stopped: {exc}\n\nSee node.log in {root}")
        raise


if __name__ == "__main__":
    # Used by the setup script: python -m aves_server.tray --autostart <path to aves-node-tray.pyw> on|off
    if len(sys.argv) != 4 or sys.argv[1] != "--autostart" or sys.argv[3] not in ("on", "off"):
        raise SystemExit("usage: python -m aves_server.tray --autostart <launcher.pyw> on|off")
    Autostart(Path(sys.argv[2]).resolve()).set(sys.argv[3] == "on")
