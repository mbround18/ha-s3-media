# S3 Media for Home Assistant

A Home Assistant **media source** backed by S3 — browse and play the photos,
camera recordings, and music already sitting in your bucket, from the Media
panel, a `media-player`, or a Picture Entity card.

Built and tested against **RustFS**, and works with any S3-compatible server —
AWS S3 itself, Ceph RGW, Cloudflare R2, Wasabi, Backblaze B2.

The S3 layer is a Rust extension module (`ha-s3-media-core`) built with
[PyO3](https://pyo3.rs) on top of `aws-config`, `aws-sdk-s3` and `tokio`.

---

## What it does

- **Browse** the bucket as folders. S3 common prefixes become directories.
- **Play** images, video and audio inline. Anything else is hidden.
- **Thumbnails** generated small, not the full photo. Where a camera embedded a
  preview in the EXIF header, that is used directly, so a tile costs kilobytes
  rather than megabytes; otherwise the image is downscaled once and cached.
- **Multiple buckets** — add one config entry per bucket/prefix pair.
- **Two delivery modes** — presigned URLs straight from the object store, or
  streaming through Home Assistant when the endpoint is not reachable from the
  browser (the usual case for a self-hosted RustFS).

## Requirements

- Home Assistant 2024.12 or newer
- A bucket, and credentials with `s3:ListBucket` and `s3:GetObject` on it

---

## Installation

### HACS (recommended)

1. HACS → **Custom repositories** → add `https://github.com/mbround18/ha-s3-media`
   with category **Integration**.
2. Install **S3 Media**, then restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → S3 Media**.

### Manual

Copy `custom_components/s3_media/` into your Home Assistant `config/custom_components/`
directory and restart.

### About the Rust extension

`manifest.json` declares `ha-s3-media-core` as a requirement, which Home
Assistant installs with pip on first setup. There is no Rust toolchain on Home
Assistant OS, so this must resolve to a **prebuilt wheel** for your
architecture. `.github/workflows/wheels.yml` builds manylinux and musllinux
wheels for x86_64, aarch64 and armv7 on every `v*` tag and publishes them to
PyPI — **you must run that release once** (or point the requirement elsewhere)
before the integration can set up on a fresh install.

If the wheel is missing, setup fails with a clear `ImportError` naming the
missing module rather than a stack trace.

---

## Configuration

| Field | Notes |
|---|---|
| **Bucket** | Bucket name. |
| **Endpoint URL** | The full base URL of your server — `http://rustfs.local:9000`. Leave empty only for AWS S3. |
| **Region** | Self-hosted endpoints generally accept `us-east-1`. |
| **Access key ID / Secret access key** | Leave both empty to fall back to the AWS default provider chain (environment, profile, instance metadata). |
| **Prefix** | Optional folder to use as the root, e.g. `photos/`. Everything above it is invisible to Home Assistant. |
| **Use path-style addressing** | Leave **on** for RustFS and most self-hosted servers. AWS S3 works either way; turn it off there if you prefer virtual-host style. |

### RustFS

RustFS speaks the S3 API, so it needs no special handling beyond the endpoint:

- **Endpoint URL**: `http://<host>:9000` (whatever your RustFS listener is)
- **Region**: `us-east-1`
- **Use path-style addressing**: on
- **Stream through Home Assistant** (options): on, unless your browser can
  reach that endpoint URL directly

That last one matters. A presigned URL points at the endpoint host, so if
Home Assistant can reach `http://rustfs.local:9000` but your phone on a
different network cannot, images load for Home Assistant and break for you.
Proxy mode routes the bytes through Home Assistant instead — slower and it uses
Home Assistant's bandwidth, but it always works, and it passes HTTP `Range`
requests through so video seeking still works. It is on by default whenever an
endpoint URL is set.

### Options

Reachable from the integration's **Configure** button.

| Option | Default | Notes |
|---|---|---|
| Media to show | Photos, Videos, Audio | Pick only Photos to browse a bucket as a photo album; video and audio in it stay hidden. |
| Stream through Home Assistant | on when an endpoint URL is set | Proxy instead of presigned URLs. |
| Show image thumbnails | on | Thumbnails are the full image; turn off for folders of large photos. |
| Show newest files first | on | Otherwise sorted by name. |
| Media link lifetime | 3600 s | How long a generated link stays valid. |
| Maximum files per folder | 1000 | Larger folders are truncated, with a warning in the log. |

---

## Layout

```
custom_components/s3_media/
  __init__.py       config entry setup, teardown, auth-failure handling
  client.py         async wrapper; every Rust call goes via the executor
  config_flow.py    setup, reauth and options flows
  const.py          keys, defaults, extension → mime-type tables
  proxy.py          media and thumbnail views: streaming, caching, Range
  thumbnail.py      EXIF preview extraction and downscaling
  media_source.py   browse and resolve
  brand/            icon and logo for the HACS/Home Assistant UI
rust/
  src/lib.rs        S3MediaClient: list, presign, head, get
```

The Rust methods block on an embedded Tokio runtime with the GIL released, so
Python calls them through `hass.async_add_executor_job` and the event loop
stays free. Listing a folder is one executor hop.

Serving an object opens a single GET and pulls the body a chunk at a time
(`open_object`), so memory does not scale with file size and the browser starts
receiving bytes immediately.

Thumbnails matter more than they look. A folder of twenty 5 MB photos was
moving ~100 MB to paint tiles a few hundred pixels wide. Now a tile is a small
JPEG: taken from the camera's embedded EXIF preview when there is one, which
needs only the first 128 KB of the file and no decoding at all, and otherwise
decoded and downscaled once, then cached on disk under `.storage`. Both routes
send an ETag derived from the object's own, so revisiting a folder is a page of
304s.

## Development

```bash
uv sync --all-groups          # builds the Rust extension in editable mode
uv run pytest
uv run ruff check custom_components tests
cargo clippy --manifest-path rust/Cargo.toml -- -D warnings
```

### Testing against a real RustFS

`docker-compose.dev.yml` brings up a throwaway RustFS. The tests in
`tests/test_live_rustfs.py` skip unless you point them at it:

```bash
docker compose -f docker-compose.dev.yml up -d
S3_MEDIA_LIVE_ENDPOINT=http://127.0.0.1:19000 uv run --with boto3 pytest
```

They cover listing, prefix/folder splitting, presigned downloads, batch
presigning, `HEAD`, full `GET`, and ranged `GET` — the whole surface the
integration relies on.

Build a wheel by hand:

```bash
uv run maturin build --release --manifest-path rust/Cargo.toml
```

## Known limits

- Folders are listed live on every browse; there is no cache. Large buckets
  browse as fast as your endpoint lists them.
- The thumbnail cache grows without bound and is never pruned. Each entry is a
  few KB, so a large library is still megabytes, but nothing evicts it.
- HEIC, HEIF and TIFF are listed as photos, but most browsers cannot display
  them, so they render as broken tiles. Convert them, or leave them out of the
  prefix you point Home Assistant at.
- Video thumbnails are not generated — that would mean decoding frames server-side.
- `max_items` truncates rather than paginating; the media browser has no paging UI.

## License

MIT — see [LICENSE](LICENSE).
