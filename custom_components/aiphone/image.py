"""Last decoded camera snapshot, available without opening a video session."""
from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_SNAPSHOT_UPDATE
from .coordinator import AiphoneCoordinator

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: AiphoneCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AiphoneLastSnapshot(hass, coord)])


class AiphoneLastSnapshot(ImageEntity):
    _attr_has_entity_name = True
    _attr_name = "Last snapshot"
    _attr_icon = "mdi:image"
    _attr_content_type = "image/jpeg"

    def __init__(self, hass: HomeAssistant, coordinator: AiphoneCoordinator) -> None:
        super().__init__(hass)
        self._coord = coordinator
        self._attr_unique_id = f"{coordinator.device_id}_last_snapshot"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.device_id)},
            connections={(CONNECTION_NETWORK_MAC, coordinator.device_mac)},
            name="Aiphone WP-2MED",
            manufacturer="Aiphone",
            model="WP-2MED",
        )
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_SNAPSHOT_UPDATE, self._on_refresh)
        )
        self._on_refresh()

    @callback
    def _on_refresh(self) -> None:
        captured = self._coord.video.snapshot.captured_at
        if captured is not None:
            self._attr_image_last_updated = datetime.fromtimestamp(captured, tz=timezone.utc)
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        return await self._coord.video.snapshot.picture()
