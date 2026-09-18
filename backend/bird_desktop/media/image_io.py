from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from .exiftool import ExifTool


HEIF_EXTENSIONS = {".heic", ".heif"}


@dataclass
class LoadedImage:
    image: Image.Image
    source_path: Path
    preview_path: Path | None


@dataclass(frozen=True)
class ImageMetadata:
    latitude: float | None
    longitude: float | None
    datetime_text: str | None


class ImageLoader:
    _heif_opener_registered = False


    @staticmethod
    def load(path: str | Path, session=None) -> LoadedImage:
        ImageLoader._register_heif_opener()

        source = Path(path)
        preview_path = (ExifTool.extract_preview(source, session=session) if session is not None else ExifTool.extract_preview(source)) if ExifTool.is_raw_file(source) else None
        open_path = preview_path or source

        try:
            with Image.open(open_path) as pil_img:
                if pil_img.format == "JPEG":
                    # Keep the decoded pixel storage when no rotation is needed.
                    # The context manager closes the file, not this pixel buffer.
                    ImageOps.exif_transpose(pil_img, in_place=True)
                    transposed = pil_img
                else:
                    transposed = ImageOps.exif_transpose(pil_img)
                if transposed.mode == "RGB":
                    image = transposed
                else:
                    try:
                        image = transposed.convert("RGB")
                    finally:
                        transposed.close()
        except Exception:
            if preview_path:
                preview_path.unlink(missing_ok=True)
            raise

        return LoadedImage(image=image, source_path=source, preview_path=preview_path)


    @staticmethod
    def read_metadata(path: str | Path) -> ImageMetadata:
        return ImageLoader._read_exiftool_metadata(Path(path))


    @staticmethod
    def cleanup(loaded: LoadedImage) -> None:
        loaded.image.close()
        if loaded.preview_path:
            loaded.preview_path.unlink(missing_ok=True)


    @staticmethod
    def _read_exiftool_metadata(path: Path) -> ImageMetadata:
        tags = ExifTool.read_tags(path)
        return ImageLoader.metadata_from_tags(tags)


    @staticmethod
    def metadata_from_tags(tags: dict) -> ImageMetadata:
        date = tags.get("DateTimeOriginal") or tags.get("CreateDate") or tags.get("DateTimeCreated")
        lat = ImageLoader._apply_gps_ref(ImageLoader._optional_float(tags.get("GPSLatitude")), tags.get("GPSLatitudeRef"), "S")
        lon = ImageLoader._apply_gps_ref(ImageLoader._optional_float(tags.get("GPSLongitude")), tags.get("GPSLongitudeRef"), "W")
        return ImageMetadata(lat, lon, str(date) if date else None)


    @staticmethod
    def _register_heif_opener() -> None:
        if ImageLoader._heif_opener_registered:
            return

        try:
            from pillow_heif import register_heif_opener
        except ImportError:
            return

        register_heif_opener()
        ImageLoader._heif_opener_registered = True


    @staticmethod
    def _apply_gps_ref(value: float | None, ref, negative_ref: str) -> float | None:
        if value is None:
            return None

        if str(ref).upper().startswith(negative_ref) and value > 0:
            return -value

        return value


    @staticmethod
    def _optional_float(value) -> float | None:
        if value in (None, ""):
            return None

        try:
            return float(value)
        except (TypeError, ValueError):
            return None
