"""Making small previews out of large photos.

The media browser shows a grid of tiles a few hundred pixels wide. Serving the
original photo for each one moves megabytes per tile, so this module produces a
small JPEG instead, by whichever of two routes is cheaper:

1. Cameras embed a postage-stamp JPEG in the EXIF header. Reading it needs only
   the first few tens of KB of the file, and no decoding at all.
2. Otherwise, decode and downscale, which needs the whole file.

Route 1 is worth the parsing code: it is the difference between transferring
~10 KB and ~5 MB per tile.
"""

from __future__ import annotations

import logging
from io import BytesIO

_LOGGER = logging.getLogger(__name__)

# Longest edge of a generated thumbnail, in pixels.
THUMBNAIL_SIZE = 512

# How much of the file to read when looking for an embedded EXIF thumbnail.
# The EXIF block sits near the start; 128 KB covers it with room to spare.
EXIF_PROBE_BYTES = 128 * 1024

_SOI = b"\xff\xd8"
_EXIF_HEADER = b"Exif\x00\x00"
_TAG_THUMBNAIL_OFFSET = 0x0201
_TAG_THUMBNAIL_LENGTH = 0x0202
_TYPE_SHORT = 3


def exif_thumbnail(data: bytes) -> bytes | None:
    """Return the JPEG embedded in a photo's EXIF header, if there is one.

    `data` only needs to be the first chunk of the file. Returns None for
    anything that is not a JPEG carrying a thumbnail, which is a normal
    outcome, not an error.
    """
    try:
        return _exif_thumbnail(data)
    except (IndexError, ValueError) as err:  # malformed or truncated header
        _LOGGER.debug("No usable EXIF thumbnail: %s", err)
        return None


def _exif_thumbnail(data: bytes) -> bytes | None:
    """Walk JPEG segments looking for the APP1/Exif block."""
    if not data.startswith(_SOI):
        return None

    position = 2
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            return None
        marker = data[position + 1]

        # Standalone markers carry no payload.
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:
            position += 2
            continue
        # Image data starts here; any EXIF block would have appeared already.
        if marker == 0xDA:
            return None

        segment_length = int.from_bytes(data[position + 2 : position + 4], "big")
        if segment_length < 2:
            return None

        if (
            marker == 0xE1
            and data[position + 4 : position + 4 + len(_EXIF_HEADER)] == _EXIF_HEADER
        ):
            tiff = data[position + 10 : position + 2 + segment_length]
            return _thumbnail_from_tiff(tiff)

        position += 2 + segment_length

    return None


def _thumbnail_from_tiff(tiff: bytes) -> bytes | None:
    """Read IFD1 of an EXIF TIFF block and slice out the thumbnail."""
    if tiff[:2] == b"II":
        endian = "little"
    elif tiff[:2] == b"MM":
        endian = "big"
    else:
        return None

    if int.from_bytes(tiff[2:4], endian) != 42:
        return None

    ifd0 = int.from_bytes(tiff[4:8], endian)
    entries = int.from_bytes(tiff[ifd0 : ifd0 + 2], endian)

    # IFD1, holding the thumbnail, is linked from the end of IFD0.
    link = ifd0 + 2 + entries * 12
    ifd1 = int.from_bytes(tiff[link : link + 4], endian)
    if not ifd1:
        return None

    offset: int | None = None
    length: int | None = None
    for index in range(int.from_bytes(tiff[ifd1 : ifd1 + 2], endian)):
        entry = ifd1 + 2 + index * 12
        tag = int.from_bytes(tiff[entry : entry + 2], endian)
        if tag not in (_TAG_THUMBNAIL_OFFSET, _TAG_THUMBNAIL_LENGTH):
            continue

        # A SHORT value occupies only the first two bytes of the value field.
        field_type = int.from_bytes(tiff[entry + 2 : entry + 4], endian)
        width = 2 if field_type == _TYPE_SHORT else 4
        value = int.from_bytes(tiff[entry + 8 : entry + 8 + width], endian)

        if tag == _TAG_THUMBNAIL_OFFSET:
            offset = value
        else:
            length = value

    if offset is None or length is None:
        return None

    thumbnail = tiff[offset : offset + length]
    return thumbnail if thumbnail.startswith(_SOI) else None


def downscale(data: bytes, max_edge: int = THUMBNAIL_SIZE) -> bytes | None:
    """Decode an image and re-encode it small. CPU-bound; run in an executor."""
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - Pillow ships with Home Assistant
        _LOGGER.warning("Pillow is unavailable, so thumbnails cannot be generated")
        return None

    try:
        with Image.open(BytesIO(data)) as image:
            # For JPEG this decodes at a reduced DCT scale, which is far
            # cheaper than decoding full size and then shrinking.
            image.draft("RGB", (max_edge, max_edge))
            image = ImageOps.exif_transpose(image) or image
            image.thumbnail((max_edge, max_edge))

            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")

            buffer = BytesIO()
            image.save(buffer, "JPEG", quality=80, optimize=True)
            return buffer.getvalue()
    except Exception as err:  # noqa: BLE001 - Pillow raises many things
        _LOGGER.debug("Could not downscale image: %s", err)
        return None
