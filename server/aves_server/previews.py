from __future__ import annotations

from pathlib import Path

from PIL import Image


THUMB_EDGE = 480
PREVIEW_EDGE = 2048


class PreviewStore:
    """JPEG previews per shot, kept in the app data volume (never next to your photos)."""

    def __init__(self, cache_dir: Path):
        self.root = cache_dir / "previews"

    def path(self, shot_id: int, kind: str) -> Path:
        if kind not in ("thumb", "preview"):
            raise ValueError(kind)
        return self.root / str(shot_id // 1000) / f"{shot_id}_{kind}.jpg"

    def save(self, shot_id: int, image: Image.Image) -> tuple[int, int]:
        """Writes both sizes from an already decoded, upright RGB image. Returns the source size."""
        for kind, edge in (("preview", PREVIEW_EDGE), ("thumb", THUMB_EDGE)):
            target = self.path(shot_id, kind)
            target.parent.mkdir(parents=True, exist_ok=True)
            copy = image.copy()
            copy.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            temporary = target.with_suffix(".tmp")
            copy.save(temporary, "JPEG", quality=82, optimize=True)
            temporary.replace(target)
            copy.close()
        return image.size

    def delete(self, shot_id: int) -> None:
        for kind in ("thumb", "preview"):
            self.path(shot_id, kind).unlink(missing_ok=True)
