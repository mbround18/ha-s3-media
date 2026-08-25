"""End-to-end tests against a live S3-compatible endpoint.

Skipped unless ``S3_MEDIA_LIVE_ENDPOINT`` is set. Bring one up with:

    docker compose -f docker-compose.dev.yml up -d
    S3_MEDIA_LIVE_ENDPOINT=http://127.0.0.1:19000 uv run pytest
"""

from __future__ import annotations

import contextlib
import os
import urllib.request

import pytest

ENDPOINT = os.environ.get("S3_MEDIA_LIVE_ENDPOINT")
ACCESS_KEY = os.environ.get("S3_MEDIA_LIVE_ACCESS_KEY", "testkey")
SECRET_KEY = os.environ.get("S3_MEDIA_LIVE_SECRET_KEY", "testsecret123")
BUCKET = os.environ.get("S3_MEDIA_LIVE_BUCKET", "ha-s3-media-tests")

pytestmark = [
    pytest.mark.skipif(not ENDPOINT, reason="S3_MEDIA_LIVE_ENDPOINT is not set"),
    # pytest-homeassistant-custom-component blocks sockets by default; these
    # tests deliberately talk to a real endpoint.
    pytest.mark.usefixtures("socket_enabled"),
]

OBJECTS = {
    "media/2024/a.jpg": b"\xff\xd8\xff" + b"A" * 500,
    "media/2024/b.mp4": b"MP4" + b"B" * 2000,
    "media/2024/notes.txt": b"not media",
    "media/2025/c.png": b"\x89PNG" + b"C" * 100,
    "media/root.flac": b"fLaC" + b"D" * 50,
}


@pytest.fixture(scope="module")
def seeded_bucket():
    """Create the bucket and objects, then hand back a Rust client."""
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
    with contextlib.suppress(s3.exceptions.BucketAlreadyOwnedByYou):
        s3.create_bucket(Bucket=BUCKET)
    for key, body in OBJECTS.items():
        s3.put_object(Bucket=BUCKET, Key=key, Body=body)

    from ha_s3_media_core import S3MediaClient

    return S3MediaClient(
        BUCKET,
        region="us-east-1",
        access_key_id=ACCESS_KEY,
        secret_access_key=SECRET_KEY,
        endpoint_url=ENDPOINT,
        force_path_style=True,
    )


def test_check_access(seeded_bucket) -> None:
    """A reachable bucket with valid credentials passes the config-flow check."""
    seeded_bucket.check_access()


def test_missing_bucket_raises() -> None:
    """A bucket that does not exist reports NotFound, not a generic failure."""
    from ha_s3_media_core import S3MediaClient, S3MediaError

    client = S3MediaClient(
        "ha-s3-media-does-not-exist",
        region="us-east-1",
        access_key_id=ACCESS_KEY,
        secret_access_key=SECRET_KEY,
        endpoint_url=ENDPOINT,
        force_path_style=True,
    )
    with pytest.raises(S3MediaError, match=r"NotFound|NoSuchBucket"):
        client.check_access()


def test_bad_credentials_raise() -> None:
    """Wrong credentials surface an auth error the config flow can recognise."""
    from ha_s3_media_core import S3MediaClient, S3MediaError

    client = S3MediaClient(
        BUCKET,
        region="us-east-1",
        access_key_id="wrong",
        secret_access_key="alsowrong",
        endpoint_url=ENDPOINT,
        force_path_style=True,
    )
    with pytest.raises(S3MediaError):
        client.check_access()


def test_list_folder_splits_prefixes_and_objects(seeded_bucket) -> None:
    """Common prefixes become folders; only same-level objects are returned."""
    page = seeded_bucket.list_folder(prefix="media/")

    assert page["folders"] == ["media/2024/", "media/2025/"]
    assert [obj["key"] for obj in page["objects"]] == ["media/root.flac"]
    assert page["next_token"] is None

    obj = page["objects"][0]
    assert obj["size"] == len(OBJECTS["media/root.flac"])
    assert obj["last_modified"] > 0
    assert obj["etag"]


def test_list_folder_descends(seeded_bucket) -> None:
    """Listing a sub-prefix returns that folder's own objects."""
    page = seeded_bucket.list_folder(prefix="media/2024/")
    assert sorted(obj["key"] for obj in page["objects"]) == [
        "media/2024/a.jpg",
        "media/2024/b.mp4",
        "media/2024/notes.txt",
    ]


def test_presigned_url_downloads(seeded_bucket) -> None:
    """A presigned URL is fetchable without any further credentials."""
    url = seeded_bucket.presign_get("media/2024/a.jpg", 300)
    assert url.startswith(ENDPOINT)
    with urllib.request.urlopen(url) as response:
        assert response.read() == OBJECTS["media/2024/a.jpg"]


def test_presign_many_matches_order(seeded_bucket) -> None:
    """Batch presigning returns one URL per key, in order."""
    keys = ["media/2024/a.jpg", "media/2025/c.png"]
    urls = seeded_bucket.presign_get_many(keys, 300)
    assert len(urls) == len(keys)
    assert all(url.startswith(ENDPOINT) for url in urls)


def test_head_object(seeded_bucket) -> None:
    """Head returns the size the proxy view needs."""
    head = seeded_bucket.head_object("media/2024/b.mp4")
    assert head["size"] == len(OBJECTS["media/2024/b.mp4"])
    assert head["etag"]


def test_get_object_full_and_range(seeded_bucket) -> None:
    """Range requests are passed through, so video seeking works via the proxy."""
    body, _ctype, content_range, length = seeded_bucket.get_object("media/2024/b.mp4")
    assert body == OBJECTS["media/2024/b.mp4"]
    assert content_range is None
    assert length == len(body)

    body, _ctype, content_range, length = seeded_bucket.get_object(
        "media/2024/b.mp4", "bytes=0-9"
    )
    assert body == OBJECTS["media/2024/b.mp4"][:10]
    assert content_range == f"bytes 0-9/{len(OBJECTS['media/2024/b.mp4'])}"
    assert length == 10
