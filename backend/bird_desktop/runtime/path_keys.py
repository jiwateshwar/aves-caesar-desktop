from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Iterable

REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def canonical_file_keys(paths: Iterable[Path]) -> dict[Path, str]:
    """Resolve ordinary files through their parent, only for this invocation.

    Inspect each leaf without following links. Reparse points, missing files and
    Windows alias spellings retain the complete Path.resolve behavior.
    """
    parents: dict[Path, Path | None] = {}
    keys: dict[Path, str] = {}
    for path in paths:
        ordinary = True
        if os.name == "nt":
            # Keep Win32 short names, extended paths, streams and trailing-dot
            # normalization on the existing resolver. Scandir returns long names.
            ordinary = (path.is_absolute() and len(path.drive) == 2 and len(str(path)) < 240
                        and not any(char in path.name for char in "~:")
                        and path.name == path.name.rstrip(" ."))
        if ordinary:
            try:
                info = path.lstat()
                ordinary = (stat.S_ISREG(info.st_mode)
                            and not (getattr(info, "st_file_attributes", 0) & REPARSE_POINT))
            except OSError:
                ordinary = False
        if ordinary:
            parent = path.parent
            if parent not in parents:
                resolved = parent.resolve()
                # Keep resolving aliased parents for every leaf, including if a
                # junction is retargeted while this iterable is being consumed.
                parents[parent] = resolved if str(resolved) == str(parent.absolute()) else None
            resolved = parents[parent]
            keys[path] = str(resolved / path.name if resolved is not None else path.resolve()).casefold()
        else:
            keys[path] = str(path.resolve()).casefold()
    return keys
