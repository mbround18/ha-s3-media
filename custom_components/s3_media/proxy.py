"""HTTP views that serve bucket objects through Home Assistant.

Two routes, with different jobs:

* `/api/s3_media/...` streams the object itself, straight from a single open
  GET, so memory stays bounded no matter how large the file is.
* `/api/s3_media_thumb/...` serves a small JPEG preview for the browser grid.
  Tiles are a few hundred pixels wide; sending the original photo for each one
  moved megabytes per tile.

Both send validators so a browser that has seen a file before gets a 304
instead of the bytes again.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path, PurePosixPath
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import STORAGE_DIR

from .const import DOMAIN, IMAGE_EXTENSIONS, MIME_TYPES
from .thumbnail import EXIF_PROBE_BYTES, downscale, exif_thumbnail

_LOGGER = logging.getLogger(__name__)

_VIEWS_REGISTERED = f"{DOMAIN}_views"

# How much to pull from the stream before writing it out.
CHUNK_SIZE = 256 * 1024

# Objects are addressed by a key that includes their ETag, so a cached copy can
# never be stale: replacing the object changes the URL.
CACHE_CONTROL = "private, max-age=604800, immutable"

THUMBNAIL_CACHE_DIR = "s3_media_thumbnails"


@callback
def async_register_proxy_view(hass: HomeAssistant) -> None:
    """Register both views once per Home Assistant instance."""
    if hass.data.get(_VIEWS_REGISTERED):
        return
    hass.http.register_view(S3MediaProxyView(hass))
    hass.http.register_view(S3MediaThumbnailView(hass))
    hass.data[_VIEWS_REGISTERED] = True


def parse_range(header: str | None) -> tuple[int, int | None] | None:
    """Parse a single `bytes=start-end` range, or None if there isn't one.

    Multi-range and suffix (`bytes=-500`) requests are treated as no range at
    all: browsers do not use them for media playback, and serving the whole
    object is a valid response to a range we decline to honour.
    """
    if not header or not header.startswith("bytes=") or "," in header:
        return None

    start_text, _, end_text = header.removeprefix("bytes=").partition("-")
    if not start_text.isdigit():
        return None

    end = int(end_text) if end_text.isdigit() else None
    return int(start_text), end


def total_from_content_range(content_range: str | None) -> int | None:
    """Pull the object's full size out of a `bytes 0-9/1234` header."""
    if not content_range or "/" not in content_range:
        return None
    total = content_range.rsplit("/", 1)[1]
    return int(total) if total.isdigit() else None


def content_type_for(path: str, reported: str | None) -> str:
    """The mime type to send.

    The extension wins over whatever the store reported: bulk-uploaded photos
    routinely carry "binary/octet-stream", and Home Assistant sets
    X-Content-Type-Options: nosniff, so a wrong type stops the browser
    rendering the image at all.
    """
    return (
        MIME_TYPES.get(PurePosixPath(path).suffix.lower())
        or reported
        or "application/octet-stream"
    )


class S3MediaProxyView(HomeAssistantView):
    """Stream objects through Home Assistant."""

    url = "/api/s3_media/{entry_id}/{path:.*}"
    name = "api:s3_media"
    # Signed paths handed out by the media source satisfy this: Home Assistant's
    # auth middleware treats a valid signature as authentication.
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Keep a reference to hass for entry lookups."""
        self.hass = hass

    async def get(
        self, request: web.Request, entry_id: str, path: str
    ) -> web.StreamResponse:
        """Stream one object, honouring any Range header the client sent."""
        bucket: Any = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if bucket is None:
            _LOGGER.error("No loaded S3 Media config entry with id %s", entry_id)
            raise web.HTTPNotFound

        requested = parse_range(request.headers.get("Range"))
        http_range = None
        if requested:
            start, end = requested
            http_range = (
                f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
            )

        try:
            stream = await bucket.async_open(path, http_range)
        except Exception as err:  # surfaced as a 404 to the client
            _LOGGER.error(
                "S3 Media could not read %s from entry %s: %s", path, entry_id, err
            )
            raise web.HTTPNotFound from err

        etag = f'"{stream.etag}"' if stream.etag else None

        # An unchanged object need not be sent again.
        if etag and request.headers.get("If-None-Match") == etag:
            await stream.close()
            return web.Response(status=304, headers={"ETag": etag})

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": content_type_for(path, stream.content_type),
            "Cache-Control": CACHE_CONTROL,
        }
        if etag:
            headers["ETag"] = etag
        if requested and stream.content_range:
            headers["Content-Range"] = stream.content_range

        response = web.StreamResponse(
            status=206 if requested and stream.content_range else 200,
            headers=headers,
        )
        if stream.content_length:
            response.content_length = stream.content_length
        await response.prepare(request)

        try:
            while (chunk := await stream.read_chunk()) is not None:
                await response.write(chunk)
        except Exception as err:  # noqa: BLE001 - headers already sent
            _LOGGER.error("S3 Media stream of %s ended early: %s", path, err)
        finally:
            await stream.close()

        await response.write_eof()
        return response


class S3MediaThumbnailView(HomeAssistantView):
    """Serve small JPEG previews, generating and caching them on demand."""

    url = "/api/s3_media_thumb/{entry_id}/{path:.*}"
    name = "api:s3_media_thumb"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Keep a reference to hass for entry lookups."""
        self.hass = hass

    def cache_path(self, entry_id: str, path: str, version: str) -> Path:
        """Where a thumbnail for this exact version of an object lives."""
        digest = hashlib.sha256(f"{entry_id}\0{path}\0{version}".encode()).hexdigest()
        return Path(
            self.hass.config.path(STORAGE_DIR, THUMBNAIL_CACHE_DIR, f"{digest}.jpg")
        )

    async def get(
        self, request: web.Request, entry_id: str, path: str
    ) -> web.StreamResponse:
        """Return a thumbnail, from cache when possible."""
        bucket: Any = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if bucket is None:
            _LOGGER.error("No loaded S3 Media config entry with id %s", entry_id)
            raise web.HTTPNotFound

        if PurePosixPath(path).suffix.lower() not in IMAGE_EXTENSIONS:
            raise web.HTTPNotFound

        # The media source stamps the object's ETag onto the URL, so a cached
        # thumbnail is only ever reused for the bytes it was made from.
        version = request.query.get("v", "")
        etag = f'"thumb-{hashlib.sha256(f"{path}{version}".encode()).hexdigest()[:32]}"'

        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag})

        cached = self.cache_path(entry_id, path, version)
        thumbnail = await self.hass.async_add_executor_job(_read_cache, cached)

        if thumbnail is None:
            async with bucket.thumbnail_semaphore:
                # Re-check: another request may have generated it while waiting.
                thumbnail = await self.hass.async_add_executor_job(_read_cache, cached)
                if thumbnail is None:
                    thumbnail = await self._async_generate(bucket, path)
                    if thumbnail is None:
                        raise web.HTTPNotFound
                    await self.hass.async_add_executor_job(
                        _write_cache, cached, thumbnail
                    )

        return web.Response(
            body=thumbnail,
            headers={
                "Content-Type": "image/jpeg",
                "Cache-Control": CACHE_CONTROL,
                "ETag": etag,
            },
        )

    async def _async_generate(self, bucket: Any, path: str) -> bytes | None:
        """Make a thumbnail, preferring the cheap route."""
        # Cheap route: cameras embed a small JPEG in the EXIF header, so only
        # the first chunk of the file is needed and nothing is decoded.
        try:
            head, _type, _range, _length = await bucket.async_get(
                path, f"bytes=0-{EXIF_PROBE_BYTES - 1}"
            )
        except Exception as err:  # noqa: BLE001 - fall through to the full read
            _LOGGER.debug("Could not probe %s for an EXIF thumbnail: %s", path, err)
        else:
            if embedded := exif_thumbnail(head):
                return embedded

        # Expensive route: pull the whole image and downscale it.
        try:
            stream = await bucket.async_open(path)
            try:
                original = await stream.read_all()
            finally:
                await stream.close()
        except Exception as err:  # noqa: BLE001 - reported as a 404
            _LOGGER.error("S3 Media could not read %s for a thumbnail: %s", path, err)
            return None

        return await self.hass.async_add_executor_job(downscale, original)


def _read_cache(path: Path) -> bytes | None:
    """Read a cached thumbnail, or None if it isn't there."""
    try:
        return path.read_bytes()
    except OSError:
        return None


def _write_cache(path: Path, data: bytes) -> None:
    """Write a thumbnail, atomically so a partial file is never served."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".part")
        temporary.write_bytes(data)
        temporary.replace(path)
    except OSError as err:
        _LOGGER.debug("Could not cache thumbnail at %s: %s", path, err)
