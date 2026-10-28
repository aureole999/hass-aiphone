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
from .const import CONF_ENABLE_UNLOCK, CONF_ENABLE_REPLY, CONF_ENABLE_RING_SENSOR
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
        self.call_kind = 1
        self._seen_rings: set[str] = set()
        self._status_task = None
        self._reply_request = None

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
        await self.video.snapshot.load()
        await self.hass.async_add_executor_job(self._materialize_certs)
        await self.hass.async_add_executor_job(self._connect)
        # This loop lives until unload; it must not join HA's startup wait set.
        self._status_task = self.entry.async_create_background_task(
            self.hass, self._watch_status(), "aiphone_status"
        )

    async def async_stop(self) -> None:
        status_task, self._status_task = self._status_task, None
        if status_task:
            status_task.cancel()
            await asyncio.gather(status_task, return_exceptions=True)
        await self.async_stop_reply()
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
        self.hass.loop.call_soon_threadsafe(self._connection_lost)

    @callback
    def _connection_lost(self):
        self.reply.cancel()
        self.call_ended()
        self.hass.async_create_task(self.video.async_hang_up(remote=True))
        self.notify_reply_state()

    def _on_message(self, client, userdata, msg):
        if len(msg.payload) > 256 * 1024:
            return
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

    @property
    def options(self):
        return getattr(self.entry, "options", {})

    @property
    def reply_enabled(self) -> bool:
        return self.options.get(CONF_ENABLE_REPLY, True)

    @property
    def unlock_enabled(self) -> bool:
        return self.options.get(CONF_ENABLE_UNLOCK, True)

    @property
    def ring_sensor_enabled(self) -> bool:
        return self.options.get(CONF_ENABLE_RING_SENSOR, True)

    @property
    def reply_active(self) -> bool:
        return self.reply.busy or self._reply_request is not None and not self._reply_request.done()

    async def _watch_status(self):
        previous = None
        while True:
            status = (self.mqtt_connected, self.video.call_status, self.reply_active)
            if previous != status:
                previous = status
                self.notify_reply_state()
            await asyncio.sleep(.25)

    async def async_play_reply(self) -> None:
        if not self.reply_enabled:
            raise RuntimeError("Quick reply is not enabled")
        filename = self.options.get(CONF_REPLY_AUDIO_FILE, DEFAULT_REPLY_AUDIO_FILE)
        directory = Path(self.hass.config.path(REPLY_AUDIO_SUBDIR))
        await self.reply.play(directory, filename)

    def start_reply(self) -> None:
        if not self.reply_enabled or not self.mqtt_connected:
            raise RuntimeError("Quick reply is unavailable")
        if self.reply_active:
            return
        self._reply_request = self.hass.async_create_task(self.async_play_reply())
        def finished(task):
            if not task.cancelled() and task.exception():
                _LOGGER.error("Quick reply failed: %s", task.exception())
            if self._reply_request is task:
                self._reply_request = None
            self.notify_reply_state()
        self._reply_request.add_done_callback(finished)
        self.notify_reply_state()

    async def async_stop_reply(self) -> None:
        task = self._reply_request
        if task and not task.done():
            task.cancel()
        await self.reply.stop()
        if task:
            await asyncio.gather(task, return_exceptions=True)

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
        ring = self._active_cid if self.doorbell_state == STATE_RINGING else None
        lease = await self.video.async_viewer_track()
        lease.stop()
        if ring and self._active_cid != ring:
            raise RuntimeError("The incoming call has ended")
        if self.video._mon_cid:
            return await self.video.async_start_monitor_talk()
        cid = self.video.session_cid
        return await self.video.async_answer_ring(cid) if cid else False

    def call_answered(self) -> None:
        self._cancel_timer()
        self._set_doorbell(STATE_ANSWERED)

    def call_ended(self, cid: str | None = None) -> None:
        if cid and self._active_cid and cid != self._active_cid:
            return
        self._active_cid = None
        self._cancel_timer()
        self._set_doorbell(STATE_IDLE)

    async def async_hang_up(self) -> None:
        await self.async_stop_reply()
        await self.video.async_hang_up()

    def async_unlock_door(self) -> None:
        """Send MID 26021 SET_UNLOCK_REQ. Unit auto-relocks after a few seconds."""
        if not self.unlock_enabled:
            raise RuntimeError("Unlock is not enabled")
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
        if not isinstance(raw, dict):
            return
        head = raw.get("HEADER", {}) or {}
        body = raw.get("BODY", {}) or {}
        if not isinstance(head, dict) or not isinstance(body, dict):
            return
        self.signal_trace.add("rx", raw)
        flat = {**head, **body}
        mid = str(head.get("MID", ""))
        cid = head.get("CID")

        if mid == "23001" and cid in self._seen_rings:
            return
        # Resolve session-bound waiters before changing state. A response for
        # another CID can never complete an acceptance or release request.
        self.video.on_signal(mid, raw)
        self.reply.on_signal(mid, raw)
        if mid == "23001" and cid:
            if len(self._seen_rings) >= 128:
                self._seen_rings.clear()
            self._seen_rings.add(cid)
            self.last_caller = flat.get("DSP1") or "?"
            self.last_event = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "dsp1": flat.get("DSP1"), "dsp2": flat.get("DSP2"),
                "cid": cid, "ckind": flat.get("CKIND"),
                "tmkind": flat.get("TMKIND"), "tmid": flat.get("TMID"),
            }
            self.call_kind = flat.get("CKIND", 1)
            self._active_cid = cid
            self._set_doorbell(STATE_RINGING)
            self._arm_timer()
            # 24000 acknowledges receipt; it does not accept the visitor.
            self.video._send("24000", cid, CKIND=self.call_kind, RSLT=200,
                             TMKIND=flat.get("TMKIND", 5), TMID=flat.get("TMID", 1281))
            self.hass.async_create_task(self._incoming_video(cid, self.last_caller))
        elif cid and cid in (self.video.session_cid, self._active_cid):
            if mid in ("23003", "23004") and head.get("CLIID") != self.cliid:
                # 23004 CALL_CANCEL_REQ is broadcast to every terminal when one
                # answers. The official app (RelayManager.onSignalRcv) silently
                # ignores it when NCID equals its own client id: it is the
                # answerer, so it neither replies 24004 nor leaves the room.
                if mid == "23004" and self._is_own_cancel(head, cid):
                    return
                self.video._send("24003" if mid == "23003" else "24004", cid,
                                 CKIND=self.call_kind, RSLT=200, REASON=head.get("REASON", 16))
                self.call_ended(cid)
                self.hass.async_create_task(self.video.async_hang_up(remote=True))
            elif mid == "24002" and str(head.get("RSLT")) == "200":
                # An official app/indoor station took the call. Never hang up
                # their conversation when releasing our own media subscription.
                sender = head.get("CLIID")
                if sender and sender != self.cliid:
                    self.call_ended(cid)
                    self.hass.async_create_task(self.video.async_hang_up(remote=True))

    def _is_own_cancel(self, head: dict[str, Any], cid: str) -> bool:
        return self.video.is_own_cancel(head, cid)

    async def _incoming_video(self, cid: str, caller: str) -> None:
        if self.video._lock.locked():
            await self.video.async_hang_up(remote=True)
        if self._active_cid == cid:
            await self.video.passive_capture(cid, caller)

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
            self.hass.async_create_task(self.video.async_hang_up(remote=True))
            self.call_ended()

    # -------------------------------------------------------------------
    # device info (used by entity DeviceInfo)
    # -------------------------------------------------------------------
    @property
    def device_id(self) -> str:
        return f"aiphone_{self.unit_mac.replace('-', '')}"

    @property
    def device_mac(self) -> str:
        return self.unit_mac.replace("-", ":")
