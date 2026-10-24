"""Starts the Featherbase GPU node in the system tray (run with pythonw.exe: no console window)."""

import sys
import traceback
from pathlib import Path

here = Path(__file__).resolve().parent

try:
    # The unzipped bundle has this file at the top; a repository clone has it in server/windows/.
    # (parent.parent never fails, even for a folder directly under C:\)
    root = next(candidate for candidate in (here, here.parent.parent) if (candidate / "server" / "aves_server").is_dir())
    sys.path[:0] = [str(root / "server"), str(root / "backend")]

    from aves_server.tray import main
except Exception:
    # pythonw.exe has no console, so a failure this early would otherwise vanish without a trace.
    (here / "node.log").open("a", encoding="utf-8").write("Could not start the tray app:\n" + traceback.format_exc())
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, f"The Featherbase GPU node could not start.\n\nDetails are in {here / 'node.log'}",
                                         "Featherbase GPU node", 0x10)
    except Exception:
        pass
    raise

main(root, Path(__file__).resolve())
