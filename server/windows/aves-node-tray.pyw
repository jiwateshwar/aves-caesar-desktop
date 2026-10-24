"""Starts the Aves Caesar GPU node in the system tray (run with pythonw.exe: no console window)."""

import sys
from pathlib import Path

here = Path(__file__).resolve().parent
# The unzipped bundle has this file at the top; a repository clone has it in server/windows/.
root = next(candidate for candidate in (here, here.parents[1]) if (candidate / "server" / "aves_server").is_dir())
sys.path[:0] = [str(root / "server"), str(root / "backend")]

from aves_server.tray import main  # noqa: E402

main(root, Path(__file__).resolve())
