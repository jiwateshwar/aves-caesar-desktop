"""Where each dumpsite's files are.

Dumpsite 1 is the stack's AVES_DUMP_DIR. Extra dumpsites are SMB shares on the NAS that the stack mounts at
/data/dumpsites/<slot> (DUMPSITE_2 ... DUMPSITE_6 in compose.yaml); the stack also passes the host paths
in AVES_DUMPSITE_SLOTS, e.g. "2=/mnt/MasterPool/Tank/Imports;3=", so the UI can say which share is which.
Adding a share therefore means setting its slot in the stack and redeploying, then naming it in Settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .config import ServerConfig
from .models import Dumpsite


SLOTS = range(2, 7)
MAIN_ID = 1


@dataclass(frozen=True)
class DumpsiteRoot:
    id: int
    name: str
    root: Path
    enabled: bool
    slot: int | None
    host_path: str | None
    read_only: bool = False

    @property
    def mounted(self) -> bool:
        """An unused slot is backed by an empty placeholder folder, so a folder alone doesn't mean a share is there."""
        return self.root.is_dir() and (self.slot is None or self.host_path is not None)


def slots_root() -> Path:
    return Path(os.environ.get("AVES_DUMPSITE_SLOTS_DIR", "/data/dumpsites"))


def slot_host_paths() -> dict[int, str]:
    """Host folders the stack mounted into each slot (empty slots are left out)."""
    result = {}
    for item in os.environ.get("AVES_DUMPSITE_SLOTS", "").split(";"):
        slot, _, path = item.partition("=")
        if slot.strip().isdigit() and path.strip():
            result[int(slot)] = path.strip()
    return result


def roots(db: OrmSession, config: ServerConfig) -> dict[int, DumpsiteRoot]:
    hosts = slot_host_paths()
    result = {}
    for site in db.scalars(select(Dumpsite).order_by(Dumpsite.id)):
        root = config.dump_dir if site.slot is None else slots_root() / str(site.slot)
        result[site.id] = DumpsiteRoot(site.id, site.name, root, site.enabled, site.slot, hosts.get(site.slot) if site.slot else None,
                                       bool(site.read_only))
    if MAIN_ID not in result:  # databases from before migration 0006 are migrated first, but stay safe
        result[MAIN_ID] = DumpsiteRoot(MAIN_ID, config.dump_dir.name, config.dump_dir, True, None, None)
    return result


def file_path(sites: dict[int, DumpsiteRoot], dumpsite_id: int | None, relpath: str) -> Path:
    site = sites.get(dumpsite_id or MAIN_ID)
    if site is None:
        raise LookupError(f"Dumpsite {dumpsite_id} no longer exists")
    return site.root / relpath


def display_path(sites: dict[int, DumpsiteRoot], dumpsite_id: int | None, relpath: str) -> str:
    site = sites.get(dumpsite_id or MAIN_ID)
    return f"{site.name if site else 'Dumpsite'}/{relpath}"


def source_of(db: OrmSession, config: ServerConfig, file) -> Path:
    """Where a media file sits in its dumpsite."""
    return file_path(roots(db, config), file.dumpsite_id, file.dump_relpath)


def available_slots(db: OrmSession) -> list[dict]:
    """Slots the stack has mounted that no dumpsite uses yet."""
    used = set(db.scalars(select(Dumpsite.slot).where(Dumpsite.slot.is_not(None))))
    hosts = slot_host_paths()
    return [{"slot": slot, "host_path": hosts[slot], "mounted": (slots_root() / str(slot)).is_dir()}
            for slot in SLOTS if slot in hosts and slot not in used]


def folder_label(site: DumpsiteRoot, folder: str) -> str:
    """Session folder filters show extra dumpsites' folders under the dumpsite's name."""
    if site.id == MAIN_ID:
        return folder
    return PurePosixPath(site.name, folder).as_posix() if folder else site.name
