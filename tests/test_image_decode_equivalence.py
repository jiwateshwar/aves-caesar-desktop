from pathlib import Path

import pytest
from PIL import Image, ImageOps

from bird_desktop.media.image_io import ImageLoader


@pytest.mark.parametrize("orientation", range(1, 9))
def test_jpeg_pixels_and_orientation_are_unchanged(tmp_path: Path, orientation):
    path = tmp_path / "bird.jpg"
    source = Image.new("RGB", (31, 23), "blue")
    source.paste("red", (1, 1, 8, 8))
    exif = source.getexif()
    exif[274] = orientation
    source.save(path, exif=exif)
    with Image.open(path) as original:
        expected = ImageOps.exif_transpose(original).convert("RGB")
    loaded = ImageLoader.load(path)
    try:
        assert loaded.image.mode == "RGB"
        assert loaded.image.size == expected.size
        assert loaded.image.tobytes() == expected.tobytes()
        assert loaded.image.getexif().get(274) == expected.getexif().get(274)
        assert getattr(loaded.image, "fp", None) is None
    finally:
        ImageLoader.cleanup(loaded)
        expected.close()


def test_non_rgb_images_are_still_converted(tmp_path: Path):
    path = tmp_path / "bird.png"
    Image.new("RGBA", (12, 12), (5, 10, 15, 0)).save(path)
    loaded = ImageLoader.load(path)
    try:
        assert loaded.image.mode == "RGB"
        assert loaded.image.getpixel((0, 0)) == (5, 10, 15)
    finally:
        ImageLoader.cleanup(loaded)


@pytest.mark.parametrize("mode,color", [("L", 83), ("CMYK", (20, 30, 40, 50))])
def test_non_rgb_jpegs_keep_conversion_and_release_file(tmp_path: Path, mode, color):
    path = tmp_path / "non-rgb.jpg"
    with Image.new(mode, (31, 23), color) as source:
        source.save(path)
    with Image.open(path) as original:
        expected = ImageOps.exif_transpose(original).convert("RGB")
    loaded = ImageLoader.load(path)
    try:
        assert loaded.image.tobytes() == expected.tobytes()
        path.unlink()  # Windows refuses this if the decoder still owns the file.
        assert loaded.image.size == (31, 23)
    finally:
        ImageLoader.cleanup(loaded)
        expected.close()
