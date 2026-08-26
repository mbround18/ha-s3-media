"""Constants for the S3 Media integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "s3_media"

CONF_BUCKET: Final = "bucket"
CONF_REGION: Final = "region"
CONF_ACCESS_KEY_ID: Final = "access_key_id"
CONF_SECRET_ACCESS_KEY: Final = "secret_access_key"
CONF_SESSION_TOKEN: Final = "session_token"
CONF_ENDPOINT_URL: Final = "endpoint_url"
CONF_FORCE_PATH_STYLE: Final = "force_path_style"
CONF_PREFIX: Final = "prefix"

# Options
CONF_URL_EXPIRY: Final = "url_expiry"
CONF_MAX_ITEMS: Final = "max_items"
CONF_NEWEST_FIRST: Final = "newest_first"
CONF_THUMBNAILS: Final = "thumbnails"
CONF_USE_PROXY: Final = "use_proxy"
CONF_MEDIA_TYPES: Final = "media_types"

DEFAULT_REGION: Final = "us-east-1"
DEFAULT_URL_EXPIRY: Final = 3600
DEFAULT_MAX_ITEMS: Final = 1000
DEFAULT_NEWEST_FIRST: Final = True
DEFAULT_THUMBNAILS: Final = True

# Path of the proxy view, formatted with the config entry id.
PROXY_URL_FORMAT: Final = "/api/s3_media/{entry_id}/{path}"

# Which kinds of media a bucket exposes. Selecting only "photo" makes the
# source behave as a photo album: videos and audio in the same bucket are
# hidden from the browser entirely.
MEDIA_TYPE_PHOTO: Final = "photo"
MEDIA_TYPE_VIDEO: Final = "video"
MEDIA_TYPE_AUDIO: Final = "audio"
DEFAULT_MEDIA_TYPES: Final = [MEDIA_TYPE_PHOTO, MEDIA_TYPE_VIDEO, MEDIA_TYPE_AUDIO]

# Keys never contain "|", so it is a safe separator between the config entry
# id and the object path inside a media source identifier.
IDENTIFIER_SEPARATOR: Final = "|"

# Extension -> (mime type, media class). Anything not listed is not browsable.
IMAGE_EXTENSIONS: Final = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

VIDEO_EXTENSIONS: Final = {
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".ts": "video/mp2t",
    ".m3u8": "application/vnd.apple.mpegurl",
}

# Every extension the integration will serve, mapped to the mime type to send.
# The object store's own content type is not trusted: bulk-uploaded photos
# routinely carry "binary/octet-stream", and Home Assistant sets
# X-Content-Type-Options: nosniff, so a wrong type means the browser refuses to
# render the image at all.
AUDIO_EXTENSIONS: Final = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
    ".wma": "audio/x-ms-wma",
}


MIME_TYPES: Final = {**IMAGE_EXTENSIONS, **VIDEO_EXTENSIONS, **AUDIO_EXTENSIONS}
