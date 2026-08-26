"""Expose configured S3 buckets as a Home Assistant media source."""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_player.errors import BrowseError
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.core import HomeAssistant

from .client import S3Bucket, S3MediaError
from .const import (
    AUDIO_EXTENSIONS,
    DOMAIN,
    IDENTIFIER_SEPARATOR,
    IMAGE_EXTENSIONS,
    MEDIA_TYPE_AUDIO,
    MEDIA_TYPE_PHOTO,
    MEDIA_TYPE_VIDEO,
    PROXY_URL_FORMAT,
    VIDEO_EXTENSIONS,
)

_LOGGER = logging.getLogger(__name__)

MEDIA_CLASS_BY_EXTENSION_MAP = (
    (IMAGE_EXTENSIONS, MediaClass.IMAGE, MediaType.IMAGE, MEDIA_TYPE_PHOTO),
    (VIDEO_EXTENSIONS, MediaClass.VIDEO, MediaType.VIDEO, MEDIA_TYPE_VIDEO),
    (AUDIO_EXTENSIONS, MediaClass.MUSIC, MediaType.MUSIC, MEDIA_TYPE_AUDIO),
)


async def async_get_media_source(hass: HomeAssistant) -> S3MediaSource:
    """Set up the S3 media source."""
    return S3MediaSource(hass)


def classify(
    key: str, media_types: set[str] | None = None
) -> tuple[str, MediaClass, MediaType] | None:
    """Return (mime type, media class, media type) for a key.

    Returns None when the extension is not media at all, or when its kind is
    excluded by `media_types` -- the option that lets a bucket be browsed as a
    photo album even though it also holds video and audio.
    """
    suffix = PurePosixPath(key).suffix.lower()
    for extensions, media_class, media_type, kind in MEDIA_CLASS_BY_EXTENSION_MAP:
        if (mime_type := extensions.get(suffix)) is not None:
            if media_types is not None and kind not in media_types:
                return None
            return mime_type, media_class, media_type
    return None


def split_identifier(identifier: str) -> tuple[str | None, str]:
    """Split ``<entry_id>|<path>`` into its parts."""
    if not identifier:
        return None, ""
    entry_id, _, path = identifier.partition(IDENTIFIER_SEPARATOR)
    return entry_id, path


def build_identifier(entry_id: str, path: str) -> str:
    """Join a config entry id and an object path into an identifier."""
    return f"{entry_id}{IDENTIFIER_SEPARATOR}{path}"


class S3MediaSource(MediaSource):
    """Browse and play media stored in S3-compatible buckets."""

    name = "S3 Media"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the source."""
        super().__init__(DOMAIN)
        self.hass = hass

    def _bucket(self, entry_id: str) -> S3Bucket:
        """Look up a configured bucket, or raise BrowseError."""
        bucket: S3Bucket | None = self.hass.data.get(DOMAIN, {}).get(entry_id)
        if bucket is None:
            raise BrowseError(f"Unknown or unloaded S3 Media config entry: {entry_id}")
        return bucket

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Turn an object into a playable URL."""
        entry_id, path = split_identifier(item.identifier)
        if not entry_id or not path or path.endswith("/"):
            raise Unresolvable(f"Not a playable object: {item.identifier}")

        try:
            bucket = self._bucket(entry_id)
        except BrowseError as err:
            raise Unresolvable(str(err)) from err

        if (classified := classify(path, bucket.media_types)) is None:
            raise Unresolvable(f"Unsupported media type: {path}")
        mime_type = classified[0]

        try:
            url = await self._async_media_url(bucket, entry_id, path)
        except S3MediaError as err:
            raise Unresolvable(f"Could not build a URL for {path}: {err}") from err

        return PlayMedia(url, mime_type)

    async def _async_media_url(self, bucket: S3Bucket, entry_id: str, path: str) -> str:
        """Return either a proxied signed path or a presigned S3 URL."""
        if not bucket.use_proxy:
            return await bucket.async_presign(path)

        return async_sign_path(
            self.hass,
            PROXY_URL_FORMAT.format(entry_id=entry_id, path=path),
            bucket.url_expiry_timedelta,
        )

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse buckets, folders and objects."""
        entry_id, path = split_identifier(item.identifier)

        if entry_id is None:
            return self._browse_root()

        bucket = self._bucket(entry_id)

        if path and not path.endswith("/"):
            return await self._browse_object(bucket, entry_id, path)

        return await self._browse_folder(bucket, entry_id, path)

    def _browse_root(self) -> BrowseMediaSource:
        """List every configured bucket."""
        buckets: dict[str, S3Bucket] = self.hass.data.get(DOMAIN, {})
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier="",
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=self.name,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=[
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=build_identifier(entry_id, ""),
                    media_class=MediaClass.DIRECTORY,
                    media_content_type="",
                    title=bucket.title,
                    can_play=False,
                    can_expand=True,
                    children_media_class=MediaClass.DIRECTORY,
                )
                for entry_id, bucket in sorted(
                    buckets.items(), key=lambda kv: kv[1].title
                )
            ],
        )

    async def _browse_folder(
        self, bucket: S3Bucket, entry_id: str, path: str
    ) -> BrowseMediaSource:
        """List one folder's sub-folders and playable objects."""
        try:
            listing = await bucket.async_list(path)
        except S3MediaError as err:
            raise BrowseError(
                f"Cannot list s3://{bucket.bucket}/{path}: {err}"
            ) from err

        children: list[BrowseMediaSource] = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=build_identifier(
                    entry_id, folder.removeprefix(bucket.root_prefix)
                ),
                media_class=MediaClass.DIRECTORY,
                media_content_type="",
                title=folder.rstrip("/").rsplit("/", 1)[-1],
                can_play=False,
                can_expand=True,
                children_media_class=MediaClass.DIRECTORY,
            )
            for folder in sorted(listing.folders)
        ]

        objects = [
            obj
            for obj in listing.objects
            if classify(obj.key, bucket.media_types) is not None
        ]
        if bucket.newest_first:
            objects.sort(key=lambda obj: obj.last_modified or 0.0, reverse=True)
        else:
            objects.sort(key=lambda obj: obj.key)

        entries = [
            (
                obj,
                classify(obj.key, bucket.media_types),
                obj.key.removeprefix(bucket.root_prefix),
            )
            for obj in objects
        ]
        thumbnails = await self._async_thumbnails(
            bucket,
            entry_id,
            [path for _obj, info, path in entries if info[1] is MediaClass.IMAGE],  # type: ignore[index]
        )

        for obj, info, child_path in entries:
            mime_type, media_class, _media_type = info  # type: ignore[misc]
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=build_identifier(entry_id, child_path),
                    media_class=media_class,
                    media_content_type=mime_type,
                    title=obj.name,
                    can_play=True,
                    can_expand=False,
                    thumbnail=thumbnails.get(child_path),
                )
            )

        title = path.rstrip("/").rsplit("/", 1)[-1] if path else bucket.title
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=build_identifier(entry_id, path),
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=title,
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=(
                children[0].media_class if children else MediaClass.DIRECTORY
            ),
        )

    async def _browse_object(
        self, bucket: S3Bucket, entry_id: str, path: str
    ) -> BrowseMediaSource:
        """Describe a single object."""
        if (classified := classify(path, bucket.media_types)) is None:
            raise BrowseError(f"Unsupported media type: {path}")
        mime_type, media_class, _media_type = classified

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=build_identifier(entry_id, path),
            media_class=media_class,
            media_content_type=mime_type,
            title=path.rsplit("/", 1)[-1],
            can_play=True,
            can_expand=False,
            thumbnail=(await self._async_thumbnails(bucket, entry_id, [path])).get(path)
            if media_class is MediaClass.IMAGE
            else None,
        )

    async def _async_thumbnails(
        self, bucket: S3Bucket, entry_id: str, paths: list[str]
    ) -> dict[str, str]:
        """Map image paths to preview URLs.

        Images are their own thumbnails. Proxy mode signs paths locally; direct
        mode presigns the whole folder in a single call rather than one
        round-trip to the executor per file.
        """
        if not bucket.thumbnails or not paths:
            return {}

        try:
            if bucket.use_proxy:
                return {
                    path: async_sign_path(
                        self.hass,
                        PROXY_URL_FORMAT.format(entry_id=entry_id, path=path),
                        bucket.url_expiry_timedelta,
                    )
                    for path in paths
                }
            urls = await bucket.async_presign_many(paths)
        except S3MediaError as err:
            _LOGGER.debug("No thumbnails for %s: %s", entry_id, err)
            return {}

        return dict(zip(paths, urls, strict=False))
