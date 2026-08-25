# ha-s3-media-core

Rust/PyO3 extension module providing the S3 access layer for the
[Home Assistant S3 Media](https://github.com/mbruno/ha-s3-media) integration.

Built on `aws-config` / `aws-sdk-s3` / `tokio`. All calls are blocking from
Python's point of view (they run on an embedded Tokio runtime with the GIL
released), so Home Assistant should call them via `async_add_executor_job`.
