from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bird_desktop.media.exiftool import ExifTool


STILL_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff"} | ExifTool.RAW_EXTENSIONS
SIDECAR_EXTENSIONS = {".xmp"}
# Analysis and previews use the first file type available in a shot.
PRIMARY_PREFERENCE = ["JPG", "HEIC", "TIFF", "PNG"]
_NORMALIZED_TYPES = {"JPEG": "JPG", "TIF": "TIFF", "HEIF": "HEIC"}
# NAS and OS clutter that must never be ingested.
IGNORED_DIRS = {"@eadir", ".recycle", "#recycle", ".trash", ".trashes", ".spotlight-v100", ".fseventsd", ".zfs", "$recycle.bin"}
IGNORED_EXIF_KEYS = {"SourceFile", "File:Directory", "File:FileAccessDate", "File:FileInodeChangeDate", "File:FilePermissions"}
_GROUP_PRIORITY = ["Composite", "EXIF", "XMP", "IPTC", "QuickTime", "File"]


def is_ingestible(path: Path) -> bool:
    name = path.name
    if name.startswith(".") or name.endswith((".partial", ".tmp")):
        return False
    return path.suffix.lower() in STILL_EXTENSIONS


def is_ignored_dir(name: str) -> bool:
    return name.startswith(".") or name.casefold() in IGNORED_DIRS


def file_type_for(path: Path) -> str:
    extension = path.suffix.lstrip(".").upper()
    return _NORMALIZED_TYPES.get(extension, extension)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class PhotoMetadata:
    captured_at: datetime | None
    exif_datetime: str | None
    latitude: float | None
    longitude: float | None
    camera_make: str | None
    camera_model: str | None
    lens_model: str | None
    focal_length: float | None
    f_number: float | None
    exposure_time: float | None
    iso: int | None
    width: int | None
    height: int | None
    exif: dict[str, Any]


class RestartingExifTool:
    """A persistent ExifTool process that is started again if it crashed or was killed after a timeout."""

    def __init__(self, executable: str, timeout: float = 180):
        self.executable = executable
        self.timeout = timeout
        self._session = None

    def execute(self, arguments: list[str]) -> bytes:
        from bird_desktop.media.exiftool_session import ExifToolSession

        if self._session is None or self._session.process.poll() is not None:
            if self._session is not None:
                self._session.close()
            self._session = ExifToolSession(self.executable, timeout=self.timeout)
        return self._session.execute(arguments)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None


class MetadataReader:
    """Reads full EXIF/XMP/IPTC metadata with one persistent ExifTool process."""

    def __init__(self, session):
        self.session = session

    def read_many(self, paths: list[Path]) -> dict[Path, PhotoMetadata]:
        if not paths:
            return {}
        output = self.session.execute(["-json", "-n", "-G", "--MakerNotes:all", *[str(path.resolve()) for path in paths]])
        records = json.loads(output.decode("utf-8", errors="replace") or "[]")
        by_path = {str(Path(record["SourceFile"]).resolve()).casefold(): record for record in records if "SourceFile" in record}
        return {path: parse_metadata(by_path.get(str(path.resolve()).casefold(), {})) for path in paths}


def parse_metadata(record: dict[str, Any]) -> PhotoMetadata:
    tags = {key: _clean(value) for key, value in record.items() if key not in IGNORED_EXIF_KEYS and not key.startswith("ExifTool:")}
    exif_datetime = _first_datetime_text(tags)
    latitude, longitude = _gps(tags)
    return PhotoMetadata(
        captured_at=parse_exif_datetime(exif_datetime),
        exif_datetime=exif_datetime,
        latitude=latitude,
        longitude=longitude,
        camera_make=_text(lookup(tags, "Make")),
        camera_model=_text(lookup(tags, "Model")),
        lens_model=_text(lookup(tags, "LensModel") or lookup(tags, "LensID") or lookup(tags, "Lens")),
        focal_length=_number(lookup(tags, "FocalLength")),
        f_number=_number(lookup(tags, "FNumber")),
        exposure_time=_number(lookup(tags, "ExposureTime")),
        iso=_integer(lookup(tags, "ISO")),
        width=_integer(lookup(tags, "ExifImageWidth") or lookup(tags, "ImageWidth")),
        height=_integer(lookup(tags, "ExifImageHeight") or lookup(tags, "ImageHeight")),
        exif=tags,
    )


def _clean(value: Any) -> Any:
    """Cameras pad text tags such as UserComment with NUL bytes, which PostgreSQL JSON can't store."""
    if isinstance(value, str):
        return value.replace("\x00", "").strip() if "\x00" in value else value
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    return value


def lookup(tags: dict[str, Any], name: str) -> Any:
    """Finds a tag regardless of its group, preferring the most reliable group."""
    for group in _GROUP_PRIORITY:
        value = tags.get(f"{group}:{name}")
        if value not in (None, ""):
            return value
    for key, value in tags.items():
        if key.rsplit(":", 1)[-1] == name and value not in (None, ""):
            return value
    return None


def parse_exif_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    candidates = [(text[:19], fmt) for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")]
    candidates += [(text[:10], fmt) for fmt in ("%Y:%m:%d", "%Y-%m-%d")]
    for candidate, fmt in candidates:
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        # "0000:00:00" fails to parse; very old dates are unset camera clocks.
        return parsed if parsed.year > 1970 else None
    return None


def _first_datetime_text(tags: dict[str, Any]) -> str | None:
    for name in ("DateTimeOriginal", "SubSecDateTimeOriginal", "CreateDate", "DateTimeCreated", "DateCreated"):
        value = lookup(tags, name)
        if value and parse_exif_datetime(str(value)) is not None:
            return str(value)[:19]
    return None


def _gps(tags: dict[str, Any]) -> tuple[float | None, float | None]:
    latitude = _number(tags.get("Composite:GPSLatitude"))
    longitude = _number(tags.get("Composite:GPSLongitude"))
    if latitude is None or longitude is None:
        latitude = _signed(_number(lookup(tags, "GPSLatitude")), lookup(tags, "GPSLatitudeRef"), "S")
        longitude = _signed(_number(lookup(tags, "GPSLongitude")), lookup(tags, "GPSLongitudeRef"), "W")
    if latitude is None or longitude is None:
        return None, None
    # Cameras without a fix often write 0,0.
    if abs(latitude) < 1e-6 and abs(longitude) < 1e-6:
        return None, None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None, None
    return latitude, longitude


def _signed(value: float | None, ref: Any, negative: str) -> float | None:
    if value is None:
        return None
    return -abs(value) if str(ref or "").upper().startswith(negative) else value


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _text(value: Any) -> str | None:
    return str(value).strip() or None if value not in (None, "") else None
