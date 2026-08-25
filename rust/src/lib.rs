//! PyO3 bindings around `aws-sdk-s3` for the Home Assistant S3 Media integration.
//!
//! Every public method blocks on an embedded Tokio runtime while releasing the
//! GIL, so Home Assistant can call them from its executor without stalling the
//! event loop.

use std::sync::Arc;
use std::time::Duration;

use aws_config::{BehaviorVersion, Region};
use aws_sdk_s3::config::{Credentials, SharedCredentialsProvider};
use aws_sdk_s3::presigning::PresigningConfig;
use aws_sdk_s3::Client;
use aws_smithy_http_client::tls::{rustls_provider::CryptoMode, Provider as TlsProvider};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use tokio::runtime::{Builder, Runtime};

// Errors raised to Python: anything the AWS SDK reports (auth, network,
// missing bucket) surfaces as `S3MediaError`.
pyo3::create_exception!(
    ha_s3_media_core,
    S3MediaError,
    pyo3::exceptions::PyException
);

fn s3_err<E: std::fmt::Display>(err: E) -> PyErr {
    S3MediaError::new_err(err.to_string())
}

/// `(body, content_type, content_range, content_length)` from a ranged GET.
type ObjectBody = (Vec<u8>, Option<String>, Option<String>, i64);

/// A configured, reusable client for one bucket.
#[pyclass(module = "ha_s3_media_core", frozen)]
pub struct S3MediaClient {
    runtime: Arc<Runtime>,
    client: Client,
    bucket: String,
}

#[pymethods]
impl S3MediaClient {
    /// Build a client.
    ///
    /// `endpoint_url` targets S3-compatible services (RustFS, Ceph, Cloudflare
    /// R2, Backblaze B2, Wasabi); leave it `None` for AWS itself. When
    /// `access_key_id`/`secret_access_key` are omitted the default AWS provider
    /// chain is used (env vars, profile, instance metadata).
    #[new]
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (
        bucket,
        region = None,
        access_key_id = None,
        secret_access_key = None,
        session_token = None,
        endpoint_url = None,
        force_path_style = false,
    ))]
    fn new(
        py: Python<'_>,
        bucket: String,
        region: Option<String>,
        access_key_id: Option<String>,
        secret_access_key: Option<String>,
        session_token: Option<String>,
        endpoint_url: Option<String>,
        force_path_style: bool,
    ) -> PyResult<Self> {
        if bucket.is_empty() {
            return Err(PyValueError::new_err("bucket must not be empty"));
        }

        let runtime = Arc::new(
            Builder::new_multi_thread()
                .worker_threads(2)
                .enable_all()
                .thread_name("ha-s3-media")
                .build()
                .map_err(|e| PyRuntimeError::new_err(format!("tokio runtime: {e}")))?,
        );

        let client = py.detach(|| {
            runtime.block_on(async {
                // The SDK's default HTTPS client is only wired up by the
                // `default-https-client` feature, which drags in aws-lc. Build
                // the ring-backed one explicitly instead.
                let http_client = aws_smithy_http_client::Builder::new()
                    .tls_provider(TlsProvider::Rustls(CryptoMode::Ring))
                    .build_https();

                let mut loader = aws_config::defaults(BehaviorVersion::latest())
                    .http_client(http_client.clone());

                if let Some(region) = region.clone() {
                    loader = loader.region(Region::new(region));
                }
                if let (Some(key), Some(secret)) =
                    (access_key_id.clone(), secret_access_key.clone())
                {
                    loader = loader.credentials_provider(SharedCredentialsProvider::new(
                        Credentials::new(key, secret, session_token.clone(), None, "ha-s3-media"),
                    ));
                }

                let shared = loader.load().await;
                let mut builder =
                    aws_sdk_s3::config::Builder::from(&shared).http_client(http_client);

                if let Some(endpoint) = endpoint_url.clone() {
                    builder = builder.endpoint_url(endpoint);
                }
                // Custom endpoints almost always need path-style addressing.
                builder = builder.force_path_style(force_path_style);

                Client::from_conf(builder.build())
            })
        });

        Ok(Self {
            runtime,
            client,
            bucket,
        })
    }

    #[getter]
    fn bucket(&self) -> &str {
        &self.bucket
    }

    /// Verify the credentials can reach the bucket. Used by the config flow.
    fn check_access(&self, py: Python<'_>) -> PyResult<()> {
        py.detach(|| {
            self.runtime.block_on(async {
                self.client
                    .head_bucket()
                    .bucket(&self.bucket)
                    .send()
                    .await
                    .map_err(|e| s3_err(e.into_service_error()))
            })
        })?;
        Ok(())
    }

    /// One page of a "directory" listing.
    ///
    /// Returns `{"folders": [str, ...], "objects": [{...}, ...],
    /// "next_token": str | None}` where `folders` are common prefixes below
    /// `prefix` and each object carries `key`, `size`, `last_modified`
    /// (epoch seconds, float) and `etag`.
    #[pyo3(signature = (prefix = String::new(), continuation_token = None, page_size = 1000))]
    fn list_folder<'py>(
        &self,
        py: Python<'py>,
        prefix: String,
        continuation_token: Option<String>,
        page_size: i32,
    ) -> PyResult<Bound<'py, PyDict>> {
        let page_size = page_size.clamp(1, 1000);

        let page = py.detach(|| {
            self.runtime.block_on(async {
                let mut req = self
                    .client
                    .list_objects_v2()
                    .bucket(&self.bucket)
                    .delimiter("/")
                    .max_keys(page_size);

                if !prefix.is_empty() {
                    req = req.prefix(&prefix);
                }
                if let Some(token) = continuation_token.clone() {
                    req = req.continuation_token(token);
                }

                req.send().await.map_err(|e| s3_err(e.into_service_error()))
            })
        })?;

        let folders: Vec<String> = page
            .common_prefixes()
            .iter()
            .filter_map(|p| p.prefix().map(str::to_owned))
            .collect();

        let objects = pyo3::types::PyList::empty(py);
        for obj in page.contents() {
            let Some(key) = obj.key() else { continue };
            // S3 represents an explicit folder marker as a zero-byte object
            // whose key ends in "/". It is not media; skip it.
            if key.ends_with('/') {
                continue;
            }
            let entry = PyDict::new(py);
            entry.set_item("key", key)?;
            entry.set_item("size", obj.size().unwrap_or(0))?;
            entry.set_item(
                "last_modified",
                obj.last_modified()
                    .and_then(|t| t.to_millis().ok())
                    .map(|ms| ms as f64 / 1000.0),
            )?;
            entry.set_item("etag", obj.e_tag().map(|t| t.trim_matches('"')))?;
            objects.append(entry)?;
        }

        let out = PyDict::new(py);
        out.set_item("folders", folders)?;
        out.set_item("objects", objects)?;
        out.set_item(
            "next_token",
            page.is_truncated()
                .unwrap_or(false)
                .then(|| page.next_continuation_token().map(str::to_owned))
                .flatten(),
        )?;
        Ok(out)
    }

    /// A presigned GET URL, valid for `expires_in` seconds (max 7 days).
    #[pyo3(signature = (key, expires_in = 3600))]
    fn presign_get(&self, py: Python<'_>, key: String, expires_in: u64) -> PyResult<String> {
        let expires_in = expires_in.clamp(1, 7 * 24 * 3600);
        let config = PresigningConfig::expires_in(Duration::from_secs(expires_in))
            .map_err(|e| PyValueError::new_err(e.to_string()))?;

        py.detach(|| {
            self.runtime.block_on(async {
                self.client
                    .get_object()
                    .bucket(&self.bucket)
                    .key(&key)
                    .presigned(config)
                    .await
                    .map(|req| req.uri().to_string())
                    .map_err(|e| s3_err(e.into_service_error()))
            })
        })
    }

    /// Presign many keys in one call, so callers crossing a thread boundary
    /// (Home Assistant's executor) pay that cost once per folder, not per file.
    #[pyo3(signature = (keys, expires_in = 3600))]
    fn presign_get_many(
        &self,
        py: Python<'_>,
        keys: Vec<String>,
        expires_in: u64,
    ) -> PyResult<Vec<String>> {
        let expires_in = expires_in.clamp(1, 7 * 24 * 3600);
        let duration = Duration::from_secs(expires_in);

        py.detach(|| {
            self.runtime.block_on(async {
                let mut urls = Vec::with_capacity(keys.len());
                for key in &keys {
                    let config = PresigningConfig::expires_in(duration)
                        .map_err(|e| PyValueError::new_err(e.to_string()))?;
                    let req = self
                        .client
                        .get_object()
                        .bucket(&self.bucket)
                        .key(key)
                        .presigned(config)
                        .await
                        .map_err(|e| s3_err(e.into_service_error()))?;
                    urls.push(req.uri().to_string());
                }
                Ok(urls)
            })
        })
    }

    /// Metadata for a single object: `size`, `content_type`, `last_modified`, `etag`.
    fn head_object<'py>(&self, py: Python<'py>, key: String) -> PyResult<Bound<'py, PyDict>> {
        let out = py.detach(|| {
            self.runtime.block_on(async {
                self.client
                    .head_object()
                    .bucket(&self.bucket)
                    .key(&key)
                    .send()
                    .await
                    .map_err(|e| s3_err(e.into_service_error()))
            })
        })?;

        let dict = PyDict::new(py);
        dict.set_item("size", out.content_length().unwrap_or(0))?;
        dict.set_item("content_type", out.content_type())?;
        dict.set_item(
            "last_modified",
            out.last_modified()
                .and_then(|t| t.to_millis().ok())
                .map(|ms| ms as f64 / 1000.0),
        )?;
        dict.set_item("etag", out.e_tag().map(|t| t.trim_matches('"')))?;
        Ok(dict)
    }

    /// Fetch an object's bytes, optionally honouring an HTTP `Range` header.
    ///
    /// Returns `(body, content_type, content_range, content_length)`. Used by
    /// the proxy view for S3-compatible endpoints (RustFS and friends) that the
    /// browser cannot reach directly.
    #[pyo3(signature = (key, range = None))]
    fn get_object(
        &self,
        py: Python<'_>,
        key: String,
        range: Option<String>,
    ) -> PyResult<ObjectBody> {
        py.detach(|| {
            self.runtime.block_on(async {
                let mut req = self.client.get_object().bucket(&self.bucket).key(&key);
                if let Some(range) = range.clone() {
                    req = req.range(range);
                }
                let out = req
                    .send()
                    .await
                    .map_err(|e| s3_err(e.into_service_error()))?;

                let content_type = out.content_type().map(str::to_owned);
                let content_range = out.content_range().map(str::to_owned);
                let content_length = out.content_length().unwrap_or(0);
                let body = out
                    .body
                    .collect()
                    .await
                    .map(|data| data.into_bytes().to_vec())
                    .map_err(s3_err)?;

                Ok((body, content_type, content_range, content_length))
            })
        })
    }
}

#[pymodule]
fn ha_s3_media_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("S3MediaError", m.py().get_type::<S3MediaError>())?;
    m.add_class::<S3MediaClient>()?;
    Ok(())
}
