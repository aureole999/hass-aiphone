"""Config flow for Aiphone — single pairing path.

UX:
  Step 1 'user'    — explain what to do, ask for terminal display name
  Step 2 'pair'    — async_show_progress while running pair_aiphone()
  Step 3 'done'    — success page (auto-create entry); errors have own steps
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_CERT_PEM,
    CONF_CLIID,
    CONF_IOT_HOST,
    CONF_RCVTPC,
    CONF_SECKEY_PEM,
    CONF_SNDTPC,
    CONF_TERMNAME,
    CONF_UNIT_MAC,
    CONF_REPLY_AUDIO_FILE,
    DEFAULT_REPLY_AUDIO_FILE,
    CONFIG_VERSION,
    DOMAIN,
)
from .reply_files import validate_reply_filename
from .pairing import (
    CloudRegistrationFailed,
    OTPVerifyFailed,
    PairingError,
    TermNameRegistrationFailed,
    TLSConnectFailed,
    WPNotInPairingMode,
    pair_aiphone,
)

_LOGGER = logging.getLogger(__name__)


class AiphoneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Aiphone."""

    VERSION = CONFIG_VERSION

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return AiphoneOptionsFlow(config_entry)

    def __init__(self) -> None:
        self._termname: str = "PiBridge"
        self._task: asyncio.Task | None = None
        self._result: dict | None = None
        self._error_key: str | None = None

    # ------------------------------------------------------------------
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._termname = user_input.get(CONF_TERMNAME, "PiBridge").strip() or "PiBridge"
            return await self.async_step_pair()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_TERMNAME, default="PiBridge"): str}
            ),
        )

    # ------------------------------------------------------------------
    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if self._task is None:
            self._task = self.hass.async_create_task(
                self.hass.async_add_executor_job(pair_aiphone, self._termname)
            )

        if not self._task.done():
            return self.async_show_progress(
                step_id="pair",
                progress_action="pairing",
                progress_task=self._task,
            )

        # Task done — capture result or error and transition
        try:
            self._result = self._task.result()
        except WPNotInPairingMode:
            self._error_key = "wp_not_in_pairing_mode"
        except TLSConnectFailed:
            self._error_key = "tls_failed"
        except OTPVerifyFailed:
            self._error_key = "otp_verify_failed"
        except CloudRegistrationFailed as e:
            self._error_key = "cloud_failed"
            _LOGGER.error("cloud registration failed: %s", e)
        except TermNameRegistrationFailed:
            self._error_key = "termname_failed"
        except PairingError as e:
            self._error_key = "unknown"
            _LOGGER.exception("pairing error: %s", e)
        except Exception as e:  # noqa: BLE001
            self._error_key = "unknown"
            _LOGGER.exception("unexpected pairing error: %s", e)
        finally:
            self._task = None

        next_step = "finish" if self._result else "failed"
        return self.async_show_progress_done(next_step_id=next_step)

    # ------------------------------------------------------------------
    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        assert self._result is not None
        unique = f"{self._result['unit_mac']}::{self._result['cliid']}"
        await self.async_set_unique_id(unique)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Aiphone WP-2MED ({self._result['unit_mac']})",
            data={
                CONF_CLIID:      self._result["cliid"],
                CONF_IOT_HOST:   self._result["iot_host"],
                CONF_SNDTPC:     self._result["sndtpc"],
                CONF_RCVTPC:     self._result["rcvtpc"],
                CONF_CERT_PEM:   self._result["cert_pem"],
                CONF_SECKEY_PEM: self._result["seckey_pem"],
                CONF_TERMNAME:   self._result["termname"],
                CONF_UNIT_MAC:   self._result["unit_mac"],
            },
        )

    # ------------------------------------------------------------------
    async def async_step_failed(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            # Reset state and try again
            self._task = None
            self._result = None
            self._error_key = None
            return await self.async_step_user()
        return self.async_show_form(
            step_id="failed",
            data_schema=vol.Schema({}),
            errors={"base": self._error_key or "unknown"},
        )


class AiphoneOptionsFlow(config_entries.OptionsFlow):
    """Select one local recording; never start a call while configuring."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input=None) -> FlowResult:
        errors = {}
        if user_input is not None:
            try:
                filename = validate_reply_filename(user_input[CONF_REPLY_AUDIO_FILE])
            except ValueError:
                errors[CONF_REPLY_AUDIO_FILE] = "invalid_reply_filename"
            else:
                return self.async_create_entry(title="", data={
                    **self._entry.options, CONF_REPLY_AUDIO_FILE: filename,
                })
        default = (user_input or self._entry.options).get(
            CONF_REPLY_AUDIO_FILE, DEFAULT_REPLY_AUDIO_FILE
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_REPLY_AUDIO_FILE, default=default): str}),
            errors=errors,
        )
