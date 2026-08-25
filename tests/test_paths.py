"""Pure-logic tests: identifiers, prefixes and media classification.

These exercise the parts that decide what the media browser shows, without
needing an S3 endpoint or the compiled extension.
"""

from __future__ import annotations

import pytest

from custom_components.s3_media.const import IDENTIFIER_SEPARATOR


def normalize_prefix(prefix: str | None) -> str:
    """Mirror of client.normalize_prefix, importable without the Rust module."""
    cleaned = (prefix or "").strip().strip("/")
    return f"{cleaned}/" if cleaned else ""


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("/", ""),
        ("photos", "photos/"),
        ("/photos/", "photos/"),
        ("  photos/2024  ", "photos/2024/"),
    ],
)
def test_normalize_prefix(raw: str | None, expected: str) -> None:
    """Prefixes end in exactly one slash, or are empty."""
    assert normalize_prefix(raw) == expected


def test_identifier_round_trip() -> None:
    """An identifier splits back into its entry id and path."""
    from custom_components.s3_media.media_source import (
        build_identifier,
        split_identifier,
    )

    identifier = build_identifier("abc123", "photos/2024/img.jpg")
    assert identifier == f"abc123{IDENTIFIER_SEPARATOR}photos/2024/img.jpg"
    assert split_identifier(identifier) == ("abc123", "photos/2024/img.jpg")
    assert split_identifier("") == (None, "")
    assert split_identifier("abc123") == ("abc123", "")


@pytest.mark.parametrize(
    ("key", "mime_type"),
    [
        ("holiday/IMG_0001.JPG", "image/jpeg"),
        ("cams/front/2024-01-01.mp4", "video/mp4"),
        ("music/track.flac", "audio/flac"),
    ],
)
def test_classify_supported(key: str, mime_type: str) -> None:
    """Known extensions map to a mime type, case-insensitively."""
    from custom_components.s3_media.media_source import classify

    result = classify(key)
    assert result is not None
    assert result[0] == mime_type


@pytest.mark.parametrize("key", ["notes.txt", "backup.tar.gz", "no_extension"])
def test_classify_unsupported(key: str) -> None:
    """Non-media objects are hidden from the browser."""
    from custom_components.s3_media.media_source import classify

    assert classify(key) is None
