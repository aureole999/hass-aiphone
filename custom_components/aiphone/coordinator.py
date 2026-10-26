"""AWS IoT MQTT connection + doorbell state machine, exposed to HA entities."""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    ALPN_PROTO,
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
    REPLY_AUDIO_SUBDIR,
    DEFAULT_IOT_HOST,
    DEFAULT_IOT_PORT,
    MONITOR_DEFAULT_DURATION_S,
    RING_TIMEOUT_S,
    SIGNAL_STATE_UPDATE,
    STATE_ANSWERED,
    STATE_IDLE,
    STATE_RINGING,
)
from .media import VideoCapture, now_sid
from .reply import ReplyPublisher
from .media_diagnostics import SignalTrace

_LOGGER = logging.getLogger(__name__)


class AiphoneCoordinator:
    """Owns the AWS IoT connection and exposes parsed state to entities."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        d = entry.data

        self.cliid: str = d[CONF_CLIID]
        self.unit_mac: str = d[CONF_UNIT_MAC]
        self.iot_host: str = d.get(CONF_IOT_HOST, DEFAULT_IOT_HOST)
        self.iot_port: int = DEFAULT_IOT_PORT
        self.sndtpc: str = d[CONF_SNDTPC]
        self.rcvtpc: str = d[CONF_RCVTPC]
        self.termname: str = d.get(CONF_TERMNAME) or "PiBridge"
        self._cert_pem: str = d[CONF_CERT_PEM]
        self._seckey_pem: str = d[CONF_SECKEY_PEM]

        self.doorbell_state: str = STATE_IDLE
        self.last_caller: str = ""
        self.last_event: dict[str, Any] = {}
        self._active_cid: str | None = None

        self._client: mqtt.Client | None = None
        self._cert_path: Path | None = None
        self._key_path: Path | None = None
        self._timer_handle: asyncio.TimerHandle | None = None
        self.signal_trace = SignalTrace(self.cliid)

        self.video = VideoCapture(self)
        self.reply = ReplyPublisher(self)

    # -------------------------------------------------------------------
    # lifecycle
    # -------------------------------------------------------------------
    async def async_start(self) -> None:
        await self.hass.async_add_executor_job(self._materialize_certs)
        await self.hass.async_add_executor_job(self._connect)

    async def async_stop(self) -> None:
        await self.reply.stop()
        await self.video.aclose()
        if self._client is not None:
            await self.hass.async_add_executor_job(self._client.loop_stop)
            await self.hass.async_add_executor_job(self._client.disconnect)
            self._client = None
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None

    def _materialize_certs(self) -> None:
        """Write cert/key to /config/aiphone/_runtime/<cliid8>/ for paho's SSL."""
        runtime = Path(self.hass.config.path("aiphone")) / "_runtime" / self.cliid[:8]
        runtime.mkdir(parents=True, exist_ok=True)
        self._cert_path = runtime / "cert.pem"
        self._key_path = runtime / "key.pem"
        self._cert_path.write_text(self._cert_pem)
        self._key_path.write_text(self._seckey_pem)
        try:
            self._key_path.chmod(0o600)
        except OSError:
            pass

    # -------------------------------------------------------------------
    # MQTT
    # -------------------------------------------------------------------
    def _connect(self) -> None:
        c = mqtt.Client(client_id=self.cliid, protocol=mqtt.MQTTv311, clean_session=False)
        ctx = ssl.create_default_context()
        ctx.load_cert_chain(certfile=str(self._cert_path), keyfile=str(self._key_path))
        ctx.set_alpn_protocols([ALPN_PROTO])
        c.tls_set_context(ctx)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.connect_async(self.iot_host, self.iot_port, keepalive=60)
        c.loop_start()
        self._client = c

    def _on_connect(self, client: mqtt.Client, userdata, flags, rc):
        if rc != 0:
            _LOGGER.error("AWS IoT CONNACK rc=%s", rc)
            return
        wildcard = self.unit_mac + "/#"
        client.subscribe(wildcard, qos=1)
        _LOGGER.info("AWS IoT connected; subscribed %s", wildcard)
        self.hass.loop.call_soon_threadsafe(self.notify_reply_state)

    def _on_disconnect(self, client, userdata, rc):
        _LOGGER.warning("AWS IoT disconnected rc=%s — paho will reconnect", rc)
        self.hass.loop.call_soon_threadsafe(self.reply.cancel)
        self.hass.loop.call_soon_threadsafe(self.notify_reply_state)

    def _on_message(self, client, userdata, msg):
        try:
            raw = json.loads(msg.payload)
        except Exception:
            return
        self.hass.loop.call_soon_threadsafe(self._dispatch, raw)

    # -------------------------------------------------------------------
    # outbound (called from media.py and entities)
    # -------------------------------------------------------------------
    def publish(self, msg: dict[str, Any]) -> bool:
        """Publish a JSON envelope to the unit's SNDTPC."""
        if self._client is None or not self._client.is_connected():
            _LOGGER.warning("publish called before MQTT client connected")
            return False
        payload = json.dumps(msg, separators=(",", ":")) + "\n"
        result = self._client.publish(self.sndtpc, payload, qos=1)
        self.signal_trace.add("tx", msg)
        return result.rc == mqtt.MQTT_ERR_SUCCESS

    @property
    def active_call_id(self) -> str | None:
        return self._active_cid

    @property
    def mqtt_connected(self) -> bool:
        return self._client is not None and self._client.is_connected()

    def notify_reply_state(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATE)

    async def async_play_reply(self) -> None:
        filename = self.entry.options.get(CONF_REPLY_AUDIO_FILE, DEFAULT_REPLY_AUDIO_FILE)
        directory = Path(self.hass.config.path(REPLY_AUDIO_SUBDIR))
        await self.reply.play(directory, filename)

    async def async_start_monitor(
        self, duration_s: int = MONITOR_DEFAULT_DURATION_S
    ) -> None:
        """Kick off an active monitor session in the background.

        Returns immediately — the 30 s capture runs as a HA task so callers
        (button presses, services) don't block the entire request.
        """
        self.hass.async_create_task(self.video.monitor_capture(duration_s))

    async def async_answer_call(self) -> bool:
        """Accept an incoming ring, or use the phone button for an active monitor.

        Ring acceptance stops ringing for other clients. Monitor uses the
        observed 23022/24022 transition instead. Signaling acceptance alone
        does not prove that audio RTP has arrived.
        """
        if self.doorbell_state != STATE_RINGING and self.video._mon_cid:
            return await self.video.async_start_monitor_talk()
        cid = self._active_cid if self.doorbell_state == STATE_RINGING else None
        if not cid:
            _LOGGER.warning("answer_call: no active call/monitor to answer")
            return False
        sent = self.publish({
            "HEADER": {
                "MID": "24000", "VER": "1.0", "SYS": "2",
                "CLIID": self.cliid,
                "CKIND": 1, "RECKIND": 0, "AVID": 0,
                "CID": cid,
                "RSLT": 200, "REASON": 0, "LEN": 0,
                "SID": now_sid(),
                "SNDTPC": self.sndtpc, "RCVTPC": self.rcvtpc,
            },
            "BODY": {},
        })
        _LOGGER.info("📞 24000 RSLT=200 answer sent for cid=...%s", cid[-30:])
        return sent

    def async_unlock_door(self) -> None:
        """Send MID 26021 SET_UNLOCK_REQ. Unit auto-relocks after a few seconds."""
        pcliid = self.sndtpc.rsplit("/", 1)[-1]
        self.publish({
            "HEADER": {
                "MID": "26021", "VER": "1.0", "SYS": "2",
                "CLIID": self.cliid, "PCLIID": pcliid,
                "RCVTPC": self.rcvtpc, "RLS": "0",
                "RSLT": 0, "LEN": 0, "SID": now_sid(),
                "AVID": 0,
            },
            "BODY": {},
        })
        _LOGGER.info("🔓 26021 SET_UNLOCK_REQ sent")

    # -------------------------------------------------------------------
    # state machine (HA loop)
    # -------------------------------------------------------------------
    @callback
    def _dispatch(self, raw: dict[str, Any]) -> None:
        self.signal_trace.add("rx", raw)
        head = raw.get("HEADER", {}) or {}
        body = raw.get("BODY", {}) or {}
        flat = {**head, **body}
        mid = str(head.get("MID", ""))
        cid = head.get("CID")

        if mid == "23001":
            self.last_caller = flat.get("DSP1") or "?"
            self.last_event = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "dsp1": flat.get("DSP1"),
                "dsp2": flat.get("DSP2"),
                "cid": cid,
                "ckind": flat.get("CKIND"),
                "tmkind": flat.get("TMKIND"),
                "tmid": flat.get("TMID"),
            }
            self._set_doorbell(STATE_RINGING)
            self._arm_timer()
            if cid:
                self._active_cid = cid
                self.hass.async_create_task(
                    self.video.passive_capture(cid, flat.get("DSP1") or "ring")
                )
        elif mid == "24000" and head.get("RSLT") == 200:
            self._set_doorbell(STATE_ANSWERED)
            self._arm_timer()
        elif mid == "24002":
            self._set_doorbell(STATE_IDLE)
            self._cancel_timer()
            if cid and cid == self._active_cid:
                self._active_cid = None
                self.hass.async_create_task(self.video.passive_finalize())

        # Forward signaling MIDs to the video capture state machines
        if mid in ("31001", "24021", "31011", "31012", "24022"):
            self.video.on_signal(mid, raw)
        self.reply.on_signal(mid, raw)

    def _set_doorbell(self, new_state: str) -> None:
        if new_state == self.doorbell_state:
            return
        self.doorbell_state = new_state
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATE)

    def _arm_timer(self) -> None:
        if self._timer_handle is not None:
            self._timer_handle.cancel()
        self._timer_handle = self.hass.loop.call_later(RING_TIMEOUT_S, self._on_timeout)

    def _cancel_timer(self) -> None:
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None

    @callback
    def _on_timeout(self) -> None:
        self._timer_handle = None
        if self.doorbell_state == STATE_RINGING:
            _LOGGER.info("doorbell timeout → idle")
            self._set_doorbell(STATE_IDLE)

    # -------------------------------------------------------------------
    # device info (used by entity DeviceInfo)
    # -------------------------------------------------------------------
    @property
    def device_id(self) -> str:
        return f"aiphone_{self.unit_mac.replace('-', '')}"

    @property
    def device_mac(self) -> str:
        return self.unit_mac.replace("-", ":")
