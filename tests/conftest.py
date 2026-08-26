"""Shared fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ for every test."""
    yield


@pytest.fixture(autouse=True)
def clean_thumbnail_cache(hass):
    """Give every test an empty thumbnail cache.

    The test config directory is shared across the run, so without this a test
    sees thumbnails another test generated and its cache counts are wrong.
    """
    from custom_components.s3_media import cache

    root = Path(cache.cache_root(hass))
    shutil.rmtree(root, ignore_errors=True)
    yield
    shutil.rmtree(root, ignore_errors=True)
