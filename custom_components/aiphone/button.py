"""Button: trigger a monitor (camera-on, no ring) session."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_STATE_UPDATE, STATE_RINGING
from .coordinator import AiphoneCoordinator
from .reply import ReplyError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: AiphoneCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        AiphoneMonitorButton(coord),
        AiphoneAnswerButton(coord),
        AiphoneHangUpButton(coord),
        AiphoneReplyButton(coord),
        AiphoneStopReplyButton(coord),
    ])


def _device_info(coord: AiphoneCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coord.device_id)},
        connections={(CONNECTION_NETWORK_MAC, coord.device_mac)},
        name="Aiphone WP-2MED",
        manufacturer="Aiphone",
        model="WP-2MED",
    )


class AiphoneMonitorButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_name = "Monitor"
    _attr_icon = "mdi:cctv"

    def __init__(self, coordinator: AiphoneCoordinator) -> None:
        self._coord = coordinator
        self._attr_unique_id = f"{coordinator.device_id}_monitor"
        self._attr_device_info = _device_info(coordinator)

    async def async_press(self) -> None:
        await self._coord.async_start_monitor()


class AiphoneAnswerButton(ButtonEntity):
    """Request acceptance of a call; audio behavior requires device validation."""

    _attr_has_entity_name = True
    _attr_name = "Answer"
    _attr_icon = "mdi:phone-incoming"

    def __init__(self, coordinator: AiphoneCoordinator) -> None:
        self._coord = coordinator
        self._attr_unique_id = f"{coordinator.device_id}_answer"
        self._attr_device_info = _device_info(coordinator)

    async def async_press(self) -> None:
        try:
            if not await self._coord.async_answer_call():
                raise HomeAssistantError("No ringing call or active Monitor is available")
        except RuntimeError as err:
            raise HomeAssistantError(str(err)) from err


class _ReplyButtonBase(ButtonEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: AiphoneCoordinator) -> None:
        self._coord = coordinator
        self._attr_device_info = _device_info(coordinator)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(
            self.hass, SIGNAL_STATE_UPDATE, self._update
        ))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()


class AiphoneReplyButton(_ReplyButtonBase):
    """Explicitly accept an incoming ring and attempt one prerecorded reply."""

    _attr_translation_key = "play_reply"
    _attr_icon = "mdi:account-voice"

    def __init__(self, coordinator: AiphoneCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_id}_play_reply"

    @property
    def available(self) -> bool:
        return (
            self._coord.mqtt_connected
            and self._coord.doorbell_state == STATE_RINGING
            and self._coord.active_call_id is not None
            and not self._coord.reply.busy
        )

    async def async_press(self) -> None:
        try:
            await self._coord.async_play_reply()
        except ReplyError as err:
            raise HomeAssistantError(str(err)) from err


class AiphoneStopReplyButton(_ReplyButtonBase):
    _attr_translation_key = "stop_reply"
    _attr_icon = "mdi:stop"

    def __init__(self, coordinator: AiphoneCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_id}_stop_reply"

    @property
    def available(self) -> bool:
        return self._coord.reply.busy

    async def async_press(self) -> None:
        await self._coord.reply.stop()


class AiphoneHangUpButton(ButtonEntity):
    """End Monitor or an answered call using the app's disconnect sequence."""

    _attr_has_entity_name = True
    _attr_name = "Hang up"
    _attr_icon = "mdi:phone-hangup"

    def __init__(self, coordinator: AiphoneCoordinator) -> None:
        self._coord = coordinator
        self._attr_unique_id = f"{coordinator.device_id}_hang_up"
        self._attr_device_info = _device_info(coordinator)

    async def async_press(self) -> None:
        await self._coord.async_hang_up()
