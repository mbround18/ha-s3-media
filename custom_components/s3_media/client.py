"""Thin async wrapper around the Rust `ha_s3_media_core` extension.

The Rust methods block (they drive an embedded Tokio runtime with the GIL
released), so every call here hops onto Home Assistant's executor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONF_ACCESS_KEY_ID,
    CONF_BUCKET,
    CONF_ENDPOINT_URL,
    CONF_FORCE_PATH_STYLE,
    CONF_MAX_ITEMS,
    CONF_MEDIA_TYPES,
    CONF_NEWEST_FIRST,
    CONF_PREFIX,
    CONF_REGION,
    CONF_SECRET_ACCESS_KEY,
    CONF_SESSION_TOKEN,
    CONF_THUMBNAILS,
    CONF_URL_EXPIRY,
    CONF_USE_PROXY,
    DEFAULT_MAX_ITEMS,
    DEFAULT_MEDIA_TYPES,
    DEFAULT_NEWEST_FIRST,
    DEFAULT_THUMBNAILS,
    DEFAULT_URL_EXPIRY,
)

_LOGGER = logging.getLogger(__name__)

try:
    from ha_s3_media_core import S3MediaClient, S3MediaError
except ImportError as err:  # pragma: no cover - depends on the installed wheel
    raise ImportError(
        "The 'ha-s3-media-core' Rust extension is not installed. Home Assistant "
        "normally installs it from the integration's manifest requirements; see "
        "the S3 Media README for prebuilt wheels."
    ) from err

__all__ = ["S3Bucket", "S3MediaError", "S3Object", "normalize_prefix"]


def normalize_prefix(prefix: str | None) -> str:
    """Return a prefix that is either empty or ends with exactly one slash."""
    cleaned = (prefix or "").strip().strip("/")
    return f"{cleaned}/" if cleaned else ""


@dataclass(slots=True)
class S3Object:
    """A single media object in the bucket."""

    key: str
    size: int
    last_modified: float | None
    etag: str | None

    @property
    def name(self) -> str:
        """The file name without its folder path."""
        return self.key.rsplit("/", 1)[-1]


@dataclass(slots=True)
class S3Listing:
    """One folder's contents: sub-folder prefixes plus objects."""

    folders: list[str]
    objects: list[S3Object]
    truncated: bool


class S3Bucket:
    """Per-config-entry handle to one bucket, scoped to a root prefix."""

    def __init__(
        self,
        hass: HomeAssistant,
        title: str,
        data: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> None:
        """Create the underlying Rust client."""
        options = options or {}
        self.hass = hass
        self.title = title
        self.bucket = data[CONF_BUCKET]
        self.root_prefix = normalize_prefix(data.get(CONF_PREFIX))
        self.url_expiry: int = options.get(CONF_URL_EXPIRY, DEFAULT_URL_EXPIRY)
        self.max_items: int = options.get(CONF_MAX_ITEMS, DEFAULT_MAX_ITEMS)
        self.newest_first: bool = options.get(CONF_NEWEST_FIRST, DEFAULT_NEWEST_FIRST)
        self.thumbnails: bool = options.get(CONF_THUMBNAILS, DEFAULT_THUMBNAILS)
        # Presigned URLs point at the S3 endpoint itself. For self-hosted
        # endpoints (RustFS and friends) that address is often unreachable from
        # the browser, so default to proxying through Home Assistant instead.
        self.media_types: set[str] = set(
            options.get(CONF_MEDIA_TYPES) or DEFAULT_MEDIA_TYPES
        )
        self.use_proxy: bool = options.get(
            CONF_USE_PROXY, bool(data.get(CONF_ENDPOINT_URL))
        )

        self._client = S3MediaClient(
            self.bucket,
            region=data.get(CONF_REGION) or None,
            access_key_id=data.get(CONF_ACCESS_KEY_ID) or None,
            secret_access_key=data.get(CONF_SECRET_ACCESS_KEY) or None,
            session_token=data.get(CONF_SESSION_TOKEN) or None,
            endpoint_url=data.get(CONF_ENDPOINT_URL) or None,
            force_path_style=bool(data.get(CONF_FORCE_PATH_STYLE, False)),
        )

    async def async_check_access(self) -> None:
        """Raise `S3MediaError` if the bucket is not reachable."""
        await self.hass.async_add_executor_job(self._client.check_access)

    async def async_list(self, path: str = "") -> S3Listing:
        """List one folder, following pagination up to `max_items` objects."""
        return await self.hass.async_add_executor_job(self._list, path)

    def _list(self, path: str) -> S3Listing:
        prefix = f"{self.root_prefix}{path}"
        folders: list[str] = []
        objects: list[S3Object] = []
        token: str | None = None
        truncated = False

        while True:
            page = self._client.list_folder(
                prefix=prefix, continuation_token=token, page_size=1000
            )
            folders.extend(page["folders"])
            objects.extend(
                S3Object(
                    key=item["key"],
                    size=item["size"],
                    last_modified=item["last_modified"],
                    etag=item["etag"],
                )
                for item in page["objects"]
            )
            token = page["next_token"]

            if token is None:
                break
            if len(objects) >= self.max_items:
                truncated = True
                _LOGGER.warning(
                    "s3://%s/%s has more than %s objects; the browser is showing "
                    "the first page only. Raise 'max_items' or split the folder",
                    self.bucket,
                    prefix,
                    self.max_items,
                )
                break

        return S3Listing(folders=folders, objects=objects, truncated=truncated)

    @property
    def url_expiry_timedelta(self) -> timedelta:
        """Signed-URL lifetime, for `async_sign_path`."""
        return timedelta(seconds=self.url_expiry)

    async def async_presign_many(self, paths: list[str]) -> list[str]:
        """Presign several paths in one executor hop."""
        if not paths:
            return []
        keys = [f"{self.root_prefix}{path}" for path in paths]
        return await self.hass.async_add_executor_job(
            self._client.presign_get_many, keys, self.url_expiry
        )

    async def async_presign(self, path: str) -> str:
        """Return a presigned GET URL for a path relative to the root prefix."""
        return await self.hass.async_add_executor_job(
            self._client.presign_get, f"{self.root_prefix}{path}", self.url_expiry
        )

    async def async_head(self, path: str) -> dict[str, Any]:
        """Return metadata for a path relative to the root prefix."""
        return await self.hass.async_add_executor_job(
            self._client.head_object, f"{self.root_prefix}{path}"
        )

    async def async_get(
        self, path: str, http_range: str | None = None
    ) -> tuple[bytes, str | None, str | None, int]:
        """Fetch object bytes, passing an HTTP Range header through to S3.

        Returns ``(body, content_type, content_range, content_length)``.
        """
        return await self.hass.async_add_executor_job(
            self._client.get_object, f"{self.root_prefix}{path}", http_range
        )
