"""A button to clear a bucket's thumbnail cache."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import cache
from .entity import S3MediaEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the button for a config entry."""
    async_add_entities([S3MediaClearCacheButton(entry, entry.runtime_data)])


class S3MediaClearCacheButton(S3MediaEntity, ButtonEntity):
    """Deletes every cached thumbnail for this bucket.

    Thumbnails are keyed by the object's ETag, so they do not go stale and
    clearing is never required for correctness -- it is for reclaiming disk, or
    for forcing regeneration after changing what a thumbnail should look like.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "clear_thumbnail_cache"

    def __init__(self, entry: ConfigEntry, bucket) -> None:
        """Set the entity's identity."""
        super().__init__(entry, bucket)
        self._attr_unique_id = f"{entry.entry_id}_clear_thumbnail_cache"

    async def async_press(self) -> None:
        """Clear the cache, then refresh the sensors that report its size."""
        directory = cache.entry_cache_dir(self.hass, self.entry.entry_id)
        removed = await self.hass.async_add_executor_job(cache.clear, directory)

        _LOGGER.info(
            "Cleared %s cached thumbnails (%s bytes) for %s",
            removed.files,
            removed.bytes,
            self.entry.title,
        )
        # Tell the diagnostic sensors to re-read the directory.
        self.hass.bus.async_fire(
            f"{self.entry.entry_id}_thumbnail_cache_cleared", {"files": removed.files}
        )
