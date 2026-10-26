"""Home Assistant diagnostics without configuration credentials or media data."""

from .const import DOMAIN
from .media_diagnostics import peer_snapshot


async def async_get_config_entry_diagnostics(hass, entry) -> dict:
    coord = hass.data[DOMAIN][entry.entry_id]
    video = coord.video
    return {
        "mqtt_connected": coord.mqtt_connected,
        "doorbell_state": coord.doorbell_state,
        "media": {
            "monitor_talk_accepted": getattr(video, "_monitor_talking", False),
            "monitor_publisher": await peer_snapshot(video._mon_pc1),
            "monitor_subscriber": await peer_snapshot(video._mon_pc2),
            "call_accepted": video._accepted,
            "microphone_active": video._talk_owner is not None,
            "uplink_frames": video._uplink.frames_sent if video._uplink else 0,
            "ring_publisher": await peer_snapshot(video._ring_publisher),
            "ring_subscriber": await peer_snapshot(video._passive_pc),
            "viewers": {kind: sum(t.kind == kind for t in video.live.viewers)
                        for kind in ("audio", "video")},
        },
        "signal_trace": list(coord.signal_trace.entries),
    }
