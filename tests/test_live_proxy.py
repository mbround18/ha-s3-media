"""The whole stack, unmocked: Home Assistant + the Rust client + a real store.

Every other Home Assistant test here substitutes a fake S3 client, so the real
extension had never actually been driven through the proxy view. That gap is
where the "buffers forever" bug lived. Skipped unless S3_MEDIA_LIVE_ENDPOINT is
set; see docker-compose.dev.yml.
"""

from __future__ import annotations

import contextlib
import os

import pytest
from homeassistant.components import media_source
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.s3_media.const import (
    CONF_ACCESS_KEY_ID,
    CONF_BUCKET,
    CONF_ENDPOINT_URL,
    CONF_FORCE_PATH_STYLE,
    CONF_REGION,
    CONF_SECRET_ACCESS_KEY,
    CONF_USE_PROXY,
    DOMAIN,
)

ENDPOINT = os.environ.get("S3_MEDIA_LIVE_ENDPOINT")
ACCESS_KEY = os.environ.get("S3_MEDIA_LIVE_ACCESS_KEY", "testkey")
SECRET_KEY = os.environ.get("S3_MEDIA_LIVE_SECRET_KEY", "testsecret123")
BUCKET = "ha-s3-media-proxy-tests"

pytestmark = [
    pytest.mark.skipif(not ENDPOINT, reason="S3_MEDIA_LIVE_ENDPOINT is not set"),
    pytest.mark.usefixtures("socket_enabled"),
]

# Big enough to span several proxy chunks, like a real phone photo.
PHOTO = "Taiwan 2026/20260316_183853.jpg"
PHOTO_BYTES = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 24000  # ~6 MB
ENTRY_ID = "01M0XQH8023TBCX4ED1RTD73MT"


@pytest.fixture
async def live_entry(hass: HomeAssistant):
    """Seed a real bucket and set the integration up against it for real."""
    boto3 = pytest.importorskip("boto3")
    from botocore.config import Config

    s3 = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )
    with contextlib.suppress(Exception):
        s3.create_bucket(Bucket=BUCKET)
    s3.put_object(Bucket=BUCKET, Key=PHOTO, Body=PHOTO_BYTES)

    assert await async_setup_component(hass, "media_source", {})

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=BUCKET,
        entry_id=ENTRY_ID,
        data={
            CONF_BUCKET: BUCKET,
            CONF_REGION: "us-east-1",
            CONF_ACCESS_KEY_ID: ACCESS_KEY,
            CONF_SECRET_ACCESS_KEY: SECRET_KEY,
            CONF_ENDPOINT_URL: ENDPOINT,
            CONF_FORCE_PATH_STYLE: True,
        },
        options={CONF_USE_PROXY: True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_browse_lists_the_real_photo(hass: HomeAssistant, live_entry) -> None:
    """Browsing reaches the real store and finds the object."""
    folder = await media_source.async_browse_media(
        hass, f"media-source://s3_media/{ENTRY_ID}|Taiwan 2026/"
    )
    assert [child.title for child in folder.children] == ["20260316_183853.jpg"]


async def test_proxy_streams_a_multi_megabyte_photo(
    hass: HomeAssistant, live_entry, hass_client_no_auth
) -> None:
    """The full object arrives, byte for byte, through the real extension."""
    uri = f"media-source://s3_media/{ENTRY_ID}|{PHOTO}"
    resolved = await media_source.async_resolve_media(hass, uri, None)

    client = await hass_client_no_auth()
    response = await client.get(resolved.url)
    body = await response.read()

    assert response.status == 200
    assert response.headers["Content-Type"] == "image/jpeg"
    assert len(body) == len(PHOTO_BYTES)
    assert body == PHOTO_BYTES


async def test_proxy_range_against_the_real_store(
    hass: HomeAssistant, live_entry, hass_client_no_auth
) -> None:
    """A Range request returns exactly that slice, so seeking works."""
    uri = f"media-source://s3_media/{ENTRY_ID}|{PHOTO}"
    resolved = await media_source.async_resolve_media(hass, uri, None)

    client = await hass_client_no_auth()
    response = await client.get(
        resolved.url, headers={"Range": "bytes=1048570-1048589"}
    )
    body = await response.read()

    assert response.status == 206
    assert body == PHOTO_BYTES[1048570:1048590]
    assert response.headers["Content-Range"] == (
        f"bytes 1048570-1048589/{len(PHOTO_BYTES)}"
    )
