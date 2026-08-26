"""End-to-end tests through a real Home Assistant instance.

These boot Home Assistant with the integration set up against a fake S3 client,
then browse, resolve, and actually fetch the resulting URL over HTTP. That last
step is the one that exercises the proxy view's status, headers and signed-path
auth -- the parts a browser is fussy about.
"""

from __future__ import annotations

from io import BytesIO
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
    CONF_MEDIA_TYPES,
    CONF_REGION,
    CONF_SECRET_ACCESS_KEY,
    CONF_USE_PROXY,
    DOMAIN,
)

# A folder name with a space, because that is what broke in the field.
FOLDER = "Taiwan 2026/"
PHOTO = f"{FOLDER}20260302_152911.jpg"


def _make_photo(width: int = 64, height: int = 64) -> bytes:
    """A real JPEG, so thumbnail generation has something to decode."""
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), (120, 90, 60)).save(buffer, "JPEG")
    return buffer.getvalue()


PHOTO_BYTES = _make_photo(320, 240)

ENTRY_DATA = {
    CONF_BUCKET: "photos",
    CONF_REGION: "us-east-1",
    CONF_ACCESS_KEY_ID: "key",
    CONF_SECRET_ACCESS_KEY: "secret",
    CONF_ENDPOINT_URL: "http://rustfs.local:9000",
    CONF_FORCE_PATH_STYLE: True,
}


class FakeStream:
    """Stands in for the Rust ObjectStream."""

    def __init__(self, data, content_range=None, etag="abc123"):
        self._data = data
        self._position = 0
        self.content_type = "binary/octet-stream"
        self.content_range = content_range
        self.content_length = len(data)
        self.etag = etag
        self.last_modified = 1787704372.0

    def next_chunk(self):
        if self._position >= len(self._data):
            return None
        chunk = self._data[self._position : self._position + 65536]
        self._position += len(chunk)
        return chunk

    def close(self):
        self._position = len(self._data)


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

    def open_object(self, key, range=None):  # mirrors the Rust signature
        if key != PHOTO:
            raise RuntimeError(f"NoSuchKey: {key}")
        if range:
            start, end = range.removeprefix("bytes=").split("-")
            start = int(start)
            end = int(end) if end else len(PHOTO_BYTES) - 1
            end = min(end, len(PHOTO_BYTES) - 1)
            return FakeStream(
                PHOTO_BYTES[start : end + 1],
                f"bytes {start}-{end}/{len(PHOTO_BYTES)}",
            )
        return FakeStream(PHOTO_BYTES)

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

    def big_open(key, range=None):  # mirrors the Rust signature
        return FakeStream(big)

    uri = f"media-source://s3_media/{setup_entry.entry_id}|{PHOTO}"
    resolved = await media_source.async_resolve_media(hass, uri, None)

    bucket = hass.data[DOMAIN][setup_entry.entry_id]
    with patch.object(bucket._client, "open_object", big_open):
        client = await hass_client_no_auth()
        response = await client.get(resolved.url)
        body = await response.read()

    assert response.status == 200
    assert len(body) == len(big)
    assert response.headers["Content-Type"] == "image/jpeg"


async def test_photo_only_hides_other_media(hass: HomeAssistant) -> None:
    """Selecting only Photos hides video and audio sitting in the same folder."""
    assert await async_setup_component(hass, "media_source", {})

    mixed = {
        "folders": [],
        "objects": [
            {"key": k, "size": 10, "last_modified": 1.0, "etag": "e"}
            for k in (
                f"{FOLDER}photo.jpg",
                f"{FOLDER}photo.png",
                f"{FOLDER}clip.mp4",
                f"{FOLDER}song.mp3",
                f"{FOLDER}notes.txt",
            )
        ],
        "next_token": None,
    }

    class MixedClient(FakeS3Client):
        def list_folder(self, prefix="", continuation_token=None, page_size=1000):
            if prefix == "":
                return {"folders": [FOLDER], "objects": [], "next_token": None}
            return mixed if prefix == FOLDER else super().list_folder(prefix)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="photos",
        data=ENTRY_DATA,
        options={CONF_USE_PROXY: True, CONF_MEDIA_TYPES: ["photo"]},
        entry_id="01PHOTOONLY0000000000000000",
    )
    entry.add_to_hass(hass)

    with patch("custom_components.s3_media.client.S3MediaClient", MixedClient):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        folder = await media_source.async_browse_media(
            hass, f"media-source://s3_media/{entry.entry_id}|{FOLDER}"
        )

    assert sorted(child.title for child in folder.children) == [
        "photo.jpg",
        "photo.png",
    ]


async def test_thumbnail_url_points_at_the_thumbnail_route(
    hass: HomeAssistant, setup_entry
) -> None:
    """Tiles are served by the thumbnail view, stamped with the object's ETag."""
    folder = await media_source.async_browse_media(
        hass, f"media-source://s3_media/{setup_entry.entry_id}|{FOLDER}"
    )
    thumbnail = folder.children[0].thumbnail

    assert thumbnail.startswith(f"/api/s3_media_thumb/{setup_entry.entry_id}/")
    assert "v=abc123" in thumbnail
    assert "authSig=" in thumbnail


async def test_thumbnail_is_small_and_cached(
    hass: HomeAssistant, setup_entry, hass_client_no_auth
) -> None:
    """A tile is a downscaled JPEG, far smaller than the original, and cached."""
    from PIL import Image

    original = _make_photo(2400, 1800)

    folder = await media_source.async_browse_media(
        hass, f"media-source://s3_media/{setup_entry.entry_id}|{FOLDER}"
    )
    url = folder.children[0].thumbnail

    bucket = hass.data[DOMAIN][setup_entry.entry_id]
    client = await hass_client_no_auth()

    with (
        patch.object(
            bucket._client, "open_object", lambda key, range=None: FakeStream(original)
        ),
        patch.object(
            bucket._client,
            "get_object",
            lambda key, range=None: (
                original,
                "binary/octet-stream",
                None,
                len(original),
            ),
        ),
    ):
        response = await client.get(url)
        body = await response.read()

    assert response.status == 200
    assert response.headers["Content-Type"] == "image/jpeg"
    assert "max-age" in response.headers["Cache-Control"]
    with Image.open(BytesIO(body)) as image:
        assert max(image.size) == 512
    assert len(body) < len(original) / 5

    # Served from the on-disk cache the second time: the store is not touched.
    def explode(*args, **kwargs):
        raise AssertionError("thumbnail should have come from the cache")

    with (
        patch.object(bucket._client, "open_object", explode),
        patch.object(bucket._client, "get_object", explode),
    ):
        again = await client.get(url)
        assert again.status == 200
        assert await again.read() == body


async def test_thumbnail_returns_304_when_unchanged(
    hass: HomeAssistant, setup_entry, hass_client_no_auth
) -> None:
    """A browser that already has the tile gets a 304, not the bytes again."""
    original = _make_photo(800, 600)

    folder = await media_source.async_browse_media(
        hass, f"media-source://s3_media/{setup_entry.entry_id}|{FOLDER}"
    )
    url = folder.children[0].thumbnail
    bucket = hass.data[DOMAIN][setup_entry.entry_id]
    client = await hass_client_no_auth()

    with (
        patch.object(
            bucket._client, "open_object", lambda key, range=None: FakeStream(original)
        ),
        patch.object(
            bucket._client,
            "get_object",
            lambda key, range=None: (
                original,
                "binary/octet-stream",
                None,
                len(original),
            ),
        ),
    ):
        first = await client.get(url)
        etag = first.headers["ETag"]

        second = await client.get(url, headers={"If-None-Match": etag})

    assert second.status == 304
    assert await second.read() == b""


async def test_media_returns_304_when_unchanged(
    hass: HomeAssistant, setup_entry, hass_client_no_auth
) -> None:
    """The full-size route also answers conditional requests."""
    uri = f"media-source://s3_media/{setup_entry.entry_id}|{PHOTO}"
    resolved = await media_source.async_resolve_media(hass, uri, None)

    client = await hass_client_no_auth()
    first = await client.get(resolved.url)
    assert first.status == 200
    etag = first.headers["ETag"]

    second = await client.get(resolved.url, headers={"If-None-Match": etag})
    assert second.status == 304
    assert await second.read() == b""


async def test_clear_cache_button_empties_the_cache(
    hass: HomeAssistant, setup_entry, hass_client_no_auth
) -> None:
    """Pressing the button deletes the cached thumbnails and updates the sensors."""
    from custom_components.s3_media import cache

    original = _make_photo(800, 600)

    folder = await media_source.async_browse_media(
        hass, f"media-source://s3_media/{setup_entry.entry_id}|{FOLDER}"
    )
    url = folder.children[0].thumbnail
    bucket = hass.data[DOMAIN][setup_entry.entry_id]

    client = await hass_client_no_auth()
    with (
        patch.object(
            bucket._client, "open_object", lambda key, range=None: FakeStream(original)
        ),
        patch.object(
            bucket._client,
            "get_object",
            lambda key, range=None: (
                original,
                "binary/octet-stream",
                None,
                len(original),
            ),
        ),
    ):
        assert (await client.get(url)).status == 200

    directory = cache.entry_cache_dir(hass, setup_entry.entry_id)
    before = await hass.async_add_executor_job(cache.stats, directory)
    assert before.files == 1
    assert before.bytes > 0

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.photos_clear_thumbnail_cache"},
        blocking=True,
    )
    await hass.async_block_till_done()

    after = await hass.async_add_executor_job(cache.stats, directory)
    assert after.files == 0
    assert hass.states.get("sensor.photos_cached_thumbnails").state == "0"


async def test_cache_sensors_exist(hass: HomeAssistant, setup_entry) -> None:
    """The diagnostic sensors are what make the button's effect visible."""
    size = hass.states.get("sensor.photos_thumbnail_cache_size")
    files = hass.states.get("sensor.photos_cached_thumbnails")

    assert size is not None
    assert files is not None
    assert files.state == "0"
