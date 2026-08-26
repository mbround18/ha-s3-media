"""HTTP proxy view for buckets whose endpoint the browser cannot reach.

Presigned URLs are the fast path: the browser talks to the object store
directly and Home Assistant never touches the bytes. That only works when the
endpoint is reachable from the browser, which is often false for a self-hosted
RustFS on an internal address. In that case media resolves to a signed path on
this view instead, and Home Assistant streams the object through.

The object is fetched in chunks and written straight to the response rather
than buffered whole. A phone photo is several megabytes; buffering meant no
byte reached the browser until the entire object had been read, and memory use
scaled with the number of images the media browser requested at once.
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, MIME_TYPES

_LOGGER = logging.getLogger(__name__)

_VIEW_REGISTERED = f"{DOMAIN}_proxy_view"

# How much to pull from the object store per round trip.
CHUNK_SIZE = 1024 * 1024


@callback
def async_register_proxy_view(hass: HomeAssistant) -> None:
    """Register the proxy view once per Home Assistant instance."""
    if hass.data.get(_VIEW_REGISTERED):
        return
    hass.http.register_view(S3MediaProxyView(hass))
    hass.data[_VIEW_REGISTERED] = True


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


class S3MediaProxyView(HomeAssistantView):
    """Serve objects through Home Assistant."""

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
        start = requested[0] if requested else 0
        requested_end = requested[1] if requested else None

        # The first chunk doubles as the size probe: its Content-Range carries
        # the object's total length, so no separate HEAD is needed.
        first_stop = start + CHUNK_SIZE - 1
        if requested_end is not None:
            first_stop = min(first_stop, requested_end)

        try:
            chunk, store_type, content_range, _length = await bucket.async_get(
                path, f"bytes={start}-{first_stop}"
            )
        except Exception as err:  # surfaced as a 404 to the client
            _LOGGER.error(
                "S3 Media could not read %s from entry %s: %s", path, entry_id, err
            )
            raise web.HTTPNotFound from err

        total = total_from_content_range(content_range) or (start + len(chunk))
        last = total - 1 if requested_end is None else min(requested_end, total - 1)

        # Prefer the extension over whatever the store reported. See MIME_TYPES.
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": (
                MIME_TYPES.get(PurePosixPath(path).suffix.lower())
                or store_type
                or "application/octet-stream"
            ),
        }
        if requested:
            headers["Content-Range"] = f"bytes {start}-{last}/{total}"

        response = web.StreamResponse(status=206 if requested else 200, headers=headers)
        response.content_length = max(0, last - start + 1)
        await response.prepare(request)

        position = start
        while position <= last:
            if position > start:
                stop = min(position + CHUNK_SIZE - 1, last)
                try:
                    chunk, _type, _range, _len = await bucket.async_get(
                        path, f"bytes={position}-{stop}"
                    )
                except Exception as err:  # noqa: BLE001 - headers already sent
                    _LOGGER.error(
                        "S3 Media stream of %s failed at byte %s: %s",
                        path,
                        position,
                        err,
                    )
                    break
            if not chunk:
                break
            # The store may hand back more than the tail needs on the last chunk.
            chunk = chunk[: last - position + 1]
            await response.write(chunk)
            position += len(chunk)

        await response.write_eof()
        return response
