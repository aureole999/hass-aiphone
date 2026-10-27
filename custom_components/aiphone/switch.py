"""Quick reply switch for dashboards and home automations."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .button import _device_info
from .const import DOMAIN, SIGNAL_STATE_UPDATE


async def async_setup_entry(hass, entry, async_add_entities):
    coord = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AiphoneQuickReply(coord)] if coord.reply_enabled else [])


class AiphoneQuickReply(SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Quick reply"
    _attr_icon = "mdi:account-voice"
    _attr_should_poll = False

    def __init__(self, coordinator):
        self._coord = coordinator
        self._attr_unique_id = f"{coordinator.device_id}_quick_reply"
        self._attr_device_info = _device_info(coordinator)

    async def async_added_to_hass(self):
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_STATE_UPDATE, self._update))

    @callback
    def _update(self):
        self.async_write_ha_state()

    @property
    def available(self):
        return self._coord.reply_enabled and self._coord.mqtt_connected

    @property
    def is_on(self):
        return self._coord.reply_active

    async def async_turn_on(self, **kwargs):
        try:
            self._coord.start_reply()
        except RuntimeError as err:
            raise HomeAssistantError(str(err)) from err

    async def async_turn_off(self, **kwargs):
        await self._coord.async_stop_reply()
