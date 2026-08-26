"""End-to-end tests through a real Home Assistant instance.

These boot Home Assistant with the integration set up against a fake S3 client,
then browse, resolve, and actually fetch the resulting URL over HTTP. That last
step is the one that exercises the proxy view's status, headers and signed-path
auth -- the parts a browser is fussy about.
"""

from __future__ import annotations

from unittest.mock import patch

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

# A folder name with a space, because that is what broke in the field.
FOLDER = "Taiwan 2026/"
PHOTO = f"{FOLDER}20260302_152911.jpg"
PHOTO_BYTES = b"\xff\xd8\xff\xe0" + b"J" * 4096

ENTRY_DATA = {
    CONF_BUCKET: "photos",
    CONF_REGION: "us-east-1",
    CONF_ACCESS_KEY_ID: "key",
    CONF_SECRET_ACCESS_KEY: "secret",
    CONF_ENDPOINT_URL: "http://rustfs.local:9000",
    CONF_FORCE_PATH_STYLE: True,
}


class FakeS3Client:
    """Stands in for the Rust extension, with the same method surface."""

    def __init__(self, bucket, **kwargs):
        self.bucket = bucket
        self.kwargs = kwargs

    def check_access(self):
        return None

    def list_folder(self, prefix="", continuation_token=None, page_size=1000):
        if prefix == "":
            return {"folders": [FOLDER], "objects": [], "next_token": None}
        if prefix == FOLDER:
            return {
                "folders": [],
                "objects": [
                    {
                        "key": PHOTO,
                        "size": len(PHOTO_BYTES),
                        "last_modified": 1787704372.0,
                        "etag": "abc123",
                    }
                ],
                "next_token": None,
            }
        return {"folders": [], "objects": [], "next_token": None}

    def presign_get(self, key, expires_in=3600):
        return f"http://rustfs.local:9000/photos/{key}?sig=x"

    def presign_get_many(self, keys, expires_in=3600):
        return [self.presign_get(k, expires_in) for k in keys]

    def head_object(self, key):
        return {
            "size": len(PHOTO_BYTES),
            # RustFS often has no stored content type for bulk-uploaded photos.
            "content_type": "binary/octet-stream",
            "last_modified": 1787704372.0,
            "etag": "abc123",
        }

    def get_object(self, key, range=None):  # mirrors the Rust signature
        if key != PHOTO:
            raise RuntimeError(f"NoSuchKey: {key}")
        if range:
            start, end = range.removeprefix("bytes=").split("-")
            start = int(start)
            end = int(end) if end else len(PHOTO_BYTES) - 1
            chunk = PHOTO_BYTES[start : end + 1]
            return (
                chunk,
                "binary/octet-stream",
                f"bytes {start}-{end}/{len(PHOTO_BYTES)}",
                len(chunk),
            )
        return (PHOTO_BYTES, "binary/octet-stream", None, len(PHOTO_BYTES))


@pytest.fixture
async def setup_entry(hass: HomeAssistant):
    """Set up the integration with the fake client and the media_source stack."""
    assert await async_setup_component(hass, "media_source", {})

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="photos",
        data=ENTRY_DATA,
        options={CONF_USE_PROXY: True},
        entry_id="01M0XQH8023TBCX4ED1RTD73MT",
    )
    entry.add_to_hass(hass)

    with patch("custom_components.s3_media.client.S3MediaClient", FakeS3Client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry


async def test_browse_root_lists_bucket(hass: HomeAssistant, setup_entry) -> None:
    """The root of the source lists each configured bucket."""
    item = await media_source.async_browse_media(hass, "media-source://s3_media")
    assert [child.title for child in item.children] == ["photos"]


async def test_browse_folder_and_photo(hass: HomeAssistant, setup_entry) -> None:
    """A folder with a space in its name lists its photos with thumbnails."""
    root = f"media-source://s3_media/{setup_entry.entry_id}|"
    folder = await media_source.async_browse_media(hass, root)
    assert [child.title for child in folder.children] == ["Taiwan 2026"]

    inner = await media_source.async_browse_media(
        hass, folder.children[0].media_content_id
    )
    assert [child.title for child in inner.children] == ["20260302_152911.jpg"]

    photo = inner.children[0]
    assert photo.can_play
    assert photo.media_content_type == "image/jpeg"
    assert photo.thumbnail is not None


async def test_resolve_returns_signed_proxy_path(
    hass: HomeAssistant, setup_entry
) -> None:
    """Resolving a photo yields a signed path on the proxy view."""
    uri = f"media-source://s3_media/{setup_entry.entry_id}|{PHOTO}"
    resolved = await media_source.async_resolve_media(hass, uri, None)

    assert resolved.mime_type == "image/jpeg"
    assert resolved.url.startswith(f"/api/s3_media/{setup_entry.entry_id}/")
    assert "authSig=" in resolved.url


async def test_proxy_view_serves_the_photo(
    hass: HomeAssistant, setup_entry, hass_client_no_auth
) -> None:
    """The signed URL actually returns the image bytes over HTTP.

    This is the step a browser performs, and the one that was failing in the
    field. It checks status, body and -- critically -- Content-Type, because
    Home Assistant sends X-Content-Type-Options: nosniff, so a wrong type stops
    the image from rendering.
    """
    uri = f"media-source://s3_media/{setup_entry.entry_id}|{PHOTO}"
    resolved = await media_source.async_resolve_media(hass, uri, None)

    client = await hass_client_no_auth()
    response = await client.get(resolved.url)

    assert response.status == 200
    assert await response.read() == PHOTO_BYTES
    assert response.headers["Content-Type"] == "image/jpeg"


async def test_proxy_view_honours_range(
    hass: HomeAssistant, setup_entry, hass_client_no_auth
) -> None:
    """Range requests come back as 206 with a Content-Range, so seeking works."""
    uri = f"media-source://s3_media/{setup_entry.entry_id}|{PHOTO}"
    resolved = await media_source.async_resolve_media(hass, uri, None)

    client = await hass_client_no_auth()
    response = await client.get(resolved.url, headers={"Range": "bytes=0-9"})

    assert response.status == 206
    assert await response.read() == PHOTO_BYTES[:10]
    assert response.headers["Content-Range"] == f"bytes 0-9/{len(PHOTO_BYTES)}"


async def test_proxy_view_serves_a_large_photo(
    hass: HomeAssistant, setup_entry, hass_client_no_auth
) -> None:
    """A realistically sized phone photo comes back whole.

    Everything else here uses a few KB. Real photos are several MB, and the view
    buffers the whole object, so this checks nothing truncates or times out.
    """
    big = b"\xff\xd8\xff\xe0" + b"P" * (8 * 1024 * 1024)

    def big_get(key, range=None):  # mirrors the Rust signature
        return (big, "binary/octet-stream", None, len(big))

    uri = f"media-source://s3_media/{setup_entry.entry_id}|{PHOTO}"
    resolved = await media_source.async_resolve_media(hass, uri, None)

    bucket = hass.data[DOMAIN][setup_entry.entry_id]
    with patch.object(bucket._client, "get_object", big_get):
        client = await hass_client_no_auth()
        response = await client.get(resolved.url)
        body = await response.read()

    assert response.status == 200
    assert len(body) == len(big)
    assert response.headers["Content-Type"] == "image/jpeg"
