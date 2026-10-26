"""Bounded, sanitized signaling and WebRTC counters for interoperability debugging."""

from __future__ import annotations

import hashlib
import re
import secrets
from collections import deque
from datetime import datetime, timezone

CONTROL_FIELDS = (
    "RSLT", "REASON", "CKIND", "RECKIND", "AVID", "TMKIND", "TMID",
    "AUDIO", "VIDEO", "MUTE", "STATE", "TYPE",
)


class SignalTrace:
    """Keep protocol structure, never credentials, topics, SDP or media content."""

    def __init__(self, client_id: str) -> None:
        self._client_id = client_id
        self._salt = secrets.token_bytes(16)
        self.entries: deque[dict] = deque(maxlen=128)

    def _identifier(self, value) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        return hashlib.blake2s(value.encode(), key=self._salt, digest_size=6).hexdigest()

    def add(self, direction: str, message: dict) -> None:
        head = message.get("HEADER") or {}
        body = message.get("BODY") or {}
        if not isinstance(head, dict) or not isinstance(body, dict):
            return
        mid = str(head.get("MID", ""))
        if not re.fullmatch(r"\d{5}", mid):
            return
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "direction": direction,
            "mid": mid,
            "session": self._identifier(head.get("CID")),
            "client": "self" if head.get("CLIID") == self._client_id else self._identifier(head.get("CLIID")),
            "body_keys": sorted(str(k)[:48] for k in body),
        }
        for name, fields in (("header", head), ("body", body)):
            entry[name] = {
                key: fields[key] for key in CONTROL_FIELDS if key in fields and (
                    type(fields[key]) in (int, bool) or (
                        isinstance(fields[key], str)
                        and re.fullmatch(r"\d{1,8}|audio|video|sendonly|recvonly|sendrecv|inactive", fields[key])
                    )
                )
            }
        self.entries.append(entry)


async def peer_snapshot(pc) -> dict:
    """Read media counters for either publisher or subscriber, without ICE secrets."""
    if pc is None:
        return {"active": False}
    result = {
        "active": True,
        "connection": pc.connectionState,
        "ice": pc.iceConnectionState,
        "tracks": [
            {"kind": t.kind, "direction": t.currentDirection,
             "receiver_state": t.receiver.track.readyState if t.receiver.track else None}
            for t in pc.getTransceivers()
        ],
    }
    try:
        report = await pc.getStats()
    except Exception as err:
        result["stats_error"] = type(err).__name__
        return result
    result["rtp"] = [
        {"type": s.type, "kind": s.kind, **{
            field: getattr(s, field) for field in (
                "packetsReceived", "packetsLost", "packetsSent", "bytesSent", "jitter"
            ) if hasattr(s, field)
        }}
        for s in report.values() if s.type in ("inbound-rtp", "outbound-rtp")
    ]
    return result
