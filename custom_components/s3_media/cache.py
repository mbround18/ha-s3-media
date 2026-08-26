"""On-disk thumbnail cache.

Thumbnails live under one directory per config entry, so a bucket's cache can
be measured and cleared on its own, and disappears with the entry.

Every function here touches the filesystem and must be called from an executor.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import STORAGE_DIR

from .const import THUMBNAIL_CACHE_DIR

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CacheStats:
    """How much disk one entry's thumbnails take."""

    files: int
    bytes: int


def cache_root(hass: HomeAssistant) -> Path:
    """The directory holding every entry's thumbnails."""
    return Path(hass.config.path(STORAGE_DIR, THUMBNAIL_CACHE_DIR))


def entry_cache_dir(hass: HomeAssistant, entry_id: str) -> Path:
    """The directory holding one entry's thumbnails."""
    return cache_root(hass) / entry_id


def thumbnail_path(hass: HomeAssistant, entry_id: str, path: str, version: str) -> Path:
    """Where the thumbnail for this exact version of an object belongs.

    The version is the object's ETag, so replacing a photo yields a different
    file rather than a stale one.
    """
    digest = hashlib.sha256(f"{path}\0{version}".encode()).hexdigest()
    return entry_cache_dir(hass, entry_id) / f"{digest}.jpg"


def read(path: Path) -> bytes | None:
    """Read a cached thumbnail, or None if it isn't there."""
    try:
        return path.read_bytes()
    except OSError:
        return None


def write(path: Path, data: bytes) -> None:
    """Write a thumbnail, atomically so a partial file is never served."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".part")
        temporary.write_bytes(data)
        temporary.replace(path)
    except OSError as err:
        _LOGGER.debug("Could not cache thumbnail at %s: %s", path, err)


def stats(directory: Path) -> CacheStats:
    """Count the cached thumbnails and add up their size."""
    files = 0
    total = 0
    try:
        for entry in directory.glob("*.jpg"):
            try:
                total += entry.stat().st_size
            except OSError:  # removed between listing and stat
                continue
            files += 1
    except OSError:  # no cache yet
        pass
    return CacheStats(files=files, bytes=total)


def clear(directory: Path) -> CacheStats:
    """Delete an entry's thumbnails, returning what was removed."""
    removed = stats(directory)
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        pass
    except OSError as err:
        _LOGGER.warning("Could not clear the thumbnail cache at %s: %s", directory, err)
    return removed
