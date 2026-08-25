"""HTTP proxy view for buckets whose endpoint the browser cannot reach.

Presigned URLs are the fast path: the browser talks to S3 directly and Home
Assistant never touches the bytes. That only works when the endpoint is
reachable from the browser, which is often false for a self-hosted RustFS on an
internal address. In that case media resolves to a signed path on
this view instead, and Home Assistant streams the object through.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_VIEW_REGISTERED = f"{DOMAIN}_proxy_view"


@callback
def async_register_proxy_view(hass: HomeAssistant) -> None:
    """Register the proxy view once per Home Assistant instance."""
    if hass.data.get(_VIEW_REGISTERED):
        return
    hass.http.register_view(S3MediaProxyView(hass))
    hass.data[_VIEW_REGISTERED] = True


class S3MediaProxyView(HomeAssistantView):
    """Serve S3 objects through Home Assistant."""

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
        """Stream one object, passing any Range header through to S3."""
        bucket: Any = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if bucket is None:
            raise web.HTTPNotFound

        http_range = request.headers.get("Range")

        try:
            body, content_type, content_range, _length = await bucket.async_get(
                path, http_range
            )
        except Exception as err:
            _LOGGER.debug("Proxy fetch failed for %s: %s", path, err)
            raise web.HTTPNotFound from err

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": content_type or "application/octet-stream",
        }
        if content_range:
            headers["Content-Range"] = content_range

        return web.Response(
            body=body,
            status=206 if content_range else 200,
            headers=headers,
        )
