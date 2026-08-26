"""Thumbnail extraction and downscaling."""

from __future__ import annotations

from io import BytesIO

import pytest

from custom_components.s3_media.thumbnail import (
    THUMBNAIL_SIZE,
    downscale,
    exif_thumbnail,
)


def make_jpeg(width: int = 1600, height: int = 1200, colour=(200, 60, 40)) -> bytes:
    """A real JPEG of a given size."""
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


def with_exif_thumbnail(thumbnail: bytes, endian: str = "little") -> bytes:
    """Build a JPEG whose EXIF header embeds `thumbnail`, as cameras do."""
    order = b"II" if endian == "little" else b"MM"

    def num(value: int, size: int) -> bytes:
        return value.to_bytes(size, endian)

    # IFD0 is empty and links to IFD1, which carries the thumbnail pointers.
    ifd0 = num(0, 2) + num(14, 4)
    thumb_offset = 44
    entries = (
        num(0x0201, 2)
        + num(4, 2)
        + num(1, 4)
        + num(thumb_offset, 4)
        + num(0x0202, 2)
        + num(4, 2)
        + num(1, 4)
        + num(len(thumbnail), 4)
    )
    ifd1 = num(2, 2) + entries + num(0, 4)
    tiff = order + num(42, 2) + num(8, 4) + ifd0 + ifd1
    assert len(tiff) == thumb_offset, len(tiff)
    tiff += thumbnail

    payload = b"Exif\x00\x00" + tiff
    app1 = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    body = make_jpeg(64, 64)[2:]  # reuse a valid JPEG body, minus its SOI
    return b"\xff\xd8" + app1 + body


@pytest.mark.parametrize("endian", ["little", "big"])
def test_exif_thumbnail_is_extracted(endian: str) -> None:
    """The embedded JPEG is recovered byte for byte, either byte order."""
    thumbnail = make_jpeg(160, 120, (10, 20, 30))
    photo = with_exif_thumbnail(thumbnail, endian)

    assert exif_thumbnail(photo) == thumbnail


def test_exif_thumbnail_found_in_a_truncated_read() -> None:
    """Only the head of the file is needed, which is the point of doing this."""
    thumbnail = make_jpeg(160, 120)
    photo = with_exif_thumbnail(thumbnail) + b"\x00" * (5 * 1024 * 1024)

    assert exif_thumbnail(photo[:131072]) == thumbnail


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not a jpeg at all",
        b"\xff\xd8\xff\xd9",
        b"\xff\xd8\xff\xe1\x00\x08Exif\x00\x00",  # EXIF present but truncated
    ],
)
def test_exif_thumbnail_absent_is_not_an_error(data: bytes) -> None:
    """Anything without a usable thumbnail returns None rather than raising."""
    assert exif_thumbnail(data) is None


def test_exif_thumbnail_on_a_plain_jpeg() -> None:
    """A photo saved without EXIF simply has no embedded thumbnail."""
    assert exif_thumbnail(make_jpeg(320, 240)) is None


def test_downscale_shrinks_and_keeps_aspect() -> None:
    """Downscaling bounds the longest edge and returns a much smaller file."""
    from PIL import Image

    original = make_jpeg(1600, 1200)
    small = downscale(original)

    assert small is not None
    with Image.open(BytesIO(small)) as image:
        assert max(image.size) == THUMBNAIL_SIZE
        assert image.size == (THUMBNAIL_SIZE, THUMBNAIL_SIZE * 1200 // 1600)
    assert len(small) < len(original)


def test_downscale_rejects_non_images() -> None:
    """Garbage in returns None, not an exception."""
    assert downscale(b"this is not an image") is None
