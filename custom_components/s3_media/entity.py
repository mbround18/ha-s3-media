"""Shared device grouping for a bucket's entities."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .client import S3Bucket
from .const import DOMAIN


class S3MediaEntity(Entity):
    """Base for the entities that belong to one configured bucket."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, bucket: S3Bucket) -> None:
        """Attach the entity to the bucket's device."""
        self.entry = entry
        self.bucket = bucket
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            entry_type=DeviceEntryType.SERVICE,
            name=entry.title,
            manufacturer="S3-compatible storage",
            model=bucket.bucket,
            configuration_url=bucket.endpoint_url or None,
        )
