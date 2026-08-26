"""Diagnostic sensors describing the thumbnail cache.

They exist so the clear button means something: without a size to look at,
there is no way to tell whether clearing is worth doing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfInformation
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import cache
from .entity import S3MediaEntity

# Walking the cache directory is cheap but pointless to repeat often: the size
# only moves when someone browses a new folder.
SCAN_INTERVAL = timedelta(minutes=5)


@dataclass(frozen=True, kw_only=True)
class S3MediaSensorDescription(SensorEntityDescription):
    """Describes a sensor and how to read it from the cache stats."""

    value: Callable[[cache.CacheStats], int]


SENSORS: tuple[S3MediaSensorDescription, ...] = (
    S3MediaSensorDescription(
        key="thumbnail_cache_size",
        translation_key="thumbnail_cache_size",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.MEGABYTES,
        suggested_display_precision=1,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda stats: stats.bytes,
    ),
    S3MediaSensorDescription(
        key="thumbnail_cache_files",
        translation_key="thumbnail_cache_files",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda stats: stats.files,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the diagnostic sensors for a config entry."""
    async_add_entities(
        (
            S3MediaCacheSensor(entry, entry.runtime_data, description)
            for description in SENSORS
        ),
        # Read the cache once up front: a diagnostic that says "unknown" until
        # the first poll is not much of a diagnostic.
        update_before_add=True,
    )


class S3MediaCacheSensor(S3MediaEntity, SensorEntity):
    """Reports one measurement of the thumbnail cache."""

    entity_description: S3MediaSensorDescription

    def __init__(
        self, entry: ConfigEntry, bucket, description: S3MediaSensorDescription
    ) -> None:
        """Set the entity's identity."""
        super().__init__(entry, bucket)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

    async def async_added_to_hass(self) -> None:
        """Refresh immediately when the cache is cleared, not on the next poll."""
        self.async_on_remove(
            self.hass.bus.async_listen(
                f"{self.entry.entry_id}_thumbnail_cache_cleared", self._handle_cleared
            )
        )

    @callback
    def _handle_cleared(self, _event) -> None:
        self.async_schedule_update_ha_state(force_refresh=True)

    async def async_update(self) -> None:
        """Walk the cache directory and report."""
        directory = cache.entry_cache_dir(self.hass, self.entry.entry_id)
        stats = await self.hass.async_add_executor_job(cache.stats, directory)
        self._attr_native_value = self.entity_description.value(stats)
