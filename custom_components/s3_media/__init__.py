"""The S3 Media integration.

Exposes an S3-compatible bucket (RustFS, Ceph, R2, Wasabi, B2, AWS S3) to
Home Assistant as a media source for photos, recordings and other media.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .client import S3Bucket, S3MediaError
from .const import DOMAIN
from .proxy import async_register_proxy_view

_LOGGER = logging.getLogger(__name__)

type S3MediaConfigEntry = ConfigEntry[S3Bucket]

AUTH_ERROR_MARKERS = (
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "AccessDenied",
    "ExpiredToken",
    "InvalidToken",
    "Forbidden",
)


async def async_setup_entry(hass: HomeAssistant, entry: S3MediaConfigEntry) -> bool:
    """Set up a bucket from a config entry."""
    try:
        bucket = await hass.async_add_executor_job(
            S3Bucket, hass, entry.title, dict(entry.data), dict(entry.options)
        )
        await bucket.async_check_access()
    except S3MediaError as err:
        message = str(err)
        if any(marker in message for marker in AUTH_ERROR_MARKERS):
            raise ConfigEntryAuthFailed(message) from err
        raise ConfigEntryNotReady(
            f"Cannot reach bucket {entry.data.get('bucket')}: {message}"
        ) from err

    entry.runtime_data = bucket
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = bucket
    async_register_proxy_view(hass)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: S3MediaConfigEntry) -> bool:
    """Unload a config entry."""
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: S3MediaConfigEntry
) -> None:
    """Rebuild the client when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
