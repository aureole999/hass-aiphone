"""Camera: native WebRTC video plus snapshots from the shared incoming feed."""
from __future__ import annotations

import logging

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    WebRTCAnswer,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SIGNAL_SNAPSHOT_UPDATE
from .coordinator import AiphoneCoordinator
from .live import BrowserVideo

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coord: AiphoneCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([AiphoneEntranceCamera(coord)])


class AiphoneEntranceCamera(Camera):
    _attr_has_entity_name = True
    _attr_name = "Entrance"
    _attr_icon = "mdi:doorbell-video"
    _attr_supported_features = CameraEntityFeature.STREAM | getattr(
        CameraEntityFeature, "TWO_WAY_AUDIO", 0
    )

    def __init__(self, coordinator: AiphoneCoordinator) -> None:
        super().__init__()
        self._coord = coordinator
        self._attr_unique_id = f"{coordinator.device_id}_entrance"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.device_id)},
            connections={(CONNECTION_NETWORK_MAC, coordinator.device_mac)},
            name="Aiphone WP-2MED",
            manufacturer="Aiphone",
            model="WP-2MED",
        )
        self._browser_video = BrowserVideo(
            getattr(coordinator.video, "async_foreground_track", coordinator.video.async_viewer_track), coordinator.video.viewer_audio_track,
            getattr(coordinator.video, "async_microphone", None),
            getattr(coordinator.video, "async_microphone_closed", None),
            getattr(coordinator.video, "async_auto_answer", None),
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_SNAPSHOT_UPDATE, self._on_refresh)
        )

    async def async_will_remove_from_hass(self) -> None:
        await self._browser_video.aclose()

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Receive-only offers monitor; an offered microphone explicitly starts talk."""
        try:
            await self._browser_video.offer(
                offer_sdp, session_id, lambda sdp: send_message(WebRTCAnswer(sdp))
            )
        except Exception:
            _LOGGER.exception("Unable to start Entrance live video")
            send_message(WebRTCError(
                "aiphone_video_failed",
                "Unable to start Aiphone audio/video. Check the integration logs and connection.",
            ))

    async def async_on_webrtc_candidate(self, session_id, candidate) -> None:
        await self._browser_video.candidate(session_id, candidate)

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        self.hass.async_create_task(self._browser_video.close(session_id))

    @callback
    def _on_refresh(self) -> None:
        self.async_write_ha_state()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return await self._coord.video.snapshot.picture()
