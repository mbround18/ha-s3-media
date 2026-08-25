"""Config flow for the S3 Media integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import S3Bucket, S3MediaError, normalize_prefix
from .const import (
    CONF_ACCESS_KEY_ID,
    CONF_BUCKET,
    CONF_ENDPOINT_URL,
    CONF_FORCE_PATH_STYLE,
    CONF_MAX_ITEMS,
    CONF_NEWEST_FIRST,
    CONF_PREFIX,
    CONF_REGION,
    CONF_SECRET_ACCESS_KEY,
    CONF_THUMBNAILS,
    CONF_URL_EXPIRY,
    CONF_USE_PROXY,
    DEFAULT_MAX_ITEMS,
    DEFAULT_NEWEST_FIRST,
    DEFAULT_REGION,
    DEFAULT_THUMBNAILS,
    DEFAULT_URL_EXPIRY,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

AUTH_ERROR_MARKERS = (
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "AccessDenied",
    "ExpiredToken",
    "InvalidToken",
    "Forbidden",
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BUCKET): str,
        vol.Optional(CONF_ENDPOINT_URL, default=""): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Optional(CONF_REGION, default=DEFAULT_REGION): str,
        vol.Required(CONF_ACCESS_KEY_ID): str,
        vol.Required(CONF_SECRET_ACCESS_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_PREFIX, default=""): str,
        vol.Optional(CONF_FORCE_PATH_STYLE, default=True): BooleanSelector(),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCESS_KEY_ID): str,
        vol.Required(CONF_SECRET_ACCESS_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


def _unique_id(data: Mapping[str, Any]) -> str:
    """Identify a bucket + prefix at a given endpoint."""
    endpoint = (data.get(CONF_ENDPOINT_URL) or "aws").rstrip("/")
    return f"{endpoint}::{data[CONF_BUCKET]}::{normalize_prefix(data.get(CONF_PREFIX))}"


def _default_title(data: Mapping[str, Any]) -> str:
    """A readable name for the entry, e.g. ``photos/2024`` or ``photos``."""
    prefix = normalize_prefix(data.get(CONF_PREFIX)).rstrip("/")
    return f"{data[CONF_BUCKET]}/{prefix}" if prefix else str(data[CONF_BUCKET])


class S3MediaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the S3 Media config flow."""

    VERSION = 1

    async def _async_validate(self, data: dict[str, Any]) -> str | None:
        """Try to reach the bucket. Returns an error key, or None on success."""
        try:
            bucket = await self.hass.async_add_executor_job(
                S3Bucket, self.hass, "validation", dict(data), {}
            )
            await bucket.async_check_access()
        except S3MediaError as err:
            message = str(err)
            _LOGGER.debug(
                "Validation failed for %s: %s", data.get(CONF_BUCKET), message
            )
            if any(marker in message for marker in AUTH_ERROR_MARKERS):
                return "invalid_auth"
            if "NoSuchBucket" in message or "NotFound" in message:
                return "bucket_not_found"
            return "cannot_connect"
        except ValueError:
            return "invalid_bucket"
        except Exception:
            _LOGGER.exception("Unexpected error validating S3 Media configuration")
            return "unknown"
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the bucket connection details."""
        errors: dict[str, str] = {}

        if user_input is not None:
            data = dict(user_input)
            data[CONF_ENDPOINT_URL] = (data.get(CONF_ENDPOINT_URL) or "").strip()
            data[CONF_PREFIX] = normalize_prefix(data.get(CONF_PREFIX))

            await self.async_set_unique_id(_unique_id(data))
            self._abort_if_unique_id_configured()

            if (error := await self._async_validate(data)) is None:
                return self.async_create_entry(title=_default_title(data), data=data)
            errors["base"] = error

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication after credentials stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect replacement credentials."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            data = {**entry.data, **user_input}
            if (error := await self._async_validate(data)) is None:
                return self.async_update_reload_and_abort(entry, data=data)
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"bucket": entry.data[CONF_BUCKET]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> S3MediaOptionsFlow:
        """Return the options flow."""
        return S3MediaOptionsFlow()


class S3MediaOptionsFlow(OptionsFlow):
    """Tune browsing and playback behaviour."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the options."""
        if user_input is not None:
            user_input[CONF_URL_EXPIRY] = int(user_input[CONF_URL_EXPIRY])
            user_input[CONF_MAX_ITEMS] = int(user_input[CONF_MAX_ITEMS])
            return self.async_create_entry(data=user_input)

        options = self.config_entry.options
        has_endpoint = bool(self.config_entry.data.get(CONF_ENDPOINT_URL))

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_USE_PROXY,
                    default=options.get(CONF_USE_PROXY, has_endpoint),
                ): BooleanSelector(),
                vol.Required(
                    CONF_THUMBNAILS,
                    default=options.get(CONF_THUMBNAILS, DEFAULT_THUMBNAILS),
                ): BooleanSelector(),
                vol.Required(
                    CONF_NEWEST_FIRST,
                    default=options.get(CONF_NEWEST_FIRST, DEFAULT_NEWEST_FIRST),
                ): BooleanSelector(),
                vol.Required(
                    CONF_URL_EXPIRY,
                    default=options.get(CONF_URL_EXPIRY, DEFAULT_URL_EXPIRY),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=60,
                        max=604800,
                        step=60,
                        unit_of_measurement="s",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_MAX_ITEMS,
                    default=options.get(CONF_MAX_ITEMS, DEFAULT_MAX_ITEMS),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=100, max=100000, step=100, mode=NumberSelectorMode.BOX
                    )
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
