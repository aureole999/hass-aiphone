"""Diagnostics must expose protocol evidence without authentication or media data."""

import json
from types import SimpleNamespace

import pytest

from aiphone_test.media_diagnostics import SignalTrace, peer_snapshot
from aiphone_test.diagnostics import async_get_config_entry_diagnostics


def test_trace_is_bounded_and_does_not_keep_sensitive_fields():
    trace = SignalTrace("my-client-secret")
    for i in range(140):
        trace.add("rx", {
            "HEADER": {"MID": "24000", "CID": "private-session", "CLIID": "other-private-client",
                       "RSLT": 200, "SNDTPC": "private/topic", "CERT": "private-cert"},
            "BODY": {"SDP": "ice-pwd:private-password", "TOKEN": "private-token",
                     "OTP": 123456, "MUTE": 0, "STATE": "private-state"},
        })
    assert len(trace.entries) == 128
    encoded = json.dumps(list(trace.entries))
    for value in ("private-", "private/", "123456", "my-client-secret"):
        assert value not in encoded
    assert trace.entries[0]["session"] == trace.entries[-1]["session"]
    assert trace.entries[-1]["header"] == {"RSLT": 200}
    assert trace.entries[-1]["body"] == {"MUTE": 0}
    assert trace.entries[-1]["body_keys"] == ["MUTE", "OTP", "SDP", "STATE", "TOKEN"]


def test_session_identifiers_are_not_correlatable_between_instances():
    a, b = SignalTrace("self"), SignalTrace("self")
    message = {"HEADER": {"MID": "30001", "CID": "session", "CLIID": "self"}}
    a.add("tx", message)
    b.add("tx", message)
    assert a.entries[0]["client"] == "self"
    assert a.entries[0]["session"] != b.entries[0]["session"]
    a.add("rx", {"HEADER": {"MID": "not-a-protocol-message"}})
    assert len(a.entries) == 1


@pytest.mark.asyncio
async def test_peer_snapshot_checks_each_connection_without_transport_secrets():
    class Peer:
        connectionState = "connected"
        iceConnectionState = "completed"

        def getTransceivers(self):
            return [SimpleNamespace(kind="audio", currentDirection="recvonly",
                                    receiver=SimpleNamespace(track=SimpleNamespace(readyState="live")))]

        async def getStats(self):
            return {
                "audio": SimpleNamespace(type="inbound-rtp", kind="audio", packetsReceived=42, jitter=0),
                "ice": SimpleNamespace(type="local-candidate", ip="private-address"),
            }

    snapshot = await peer_snapshot(Peer())
    assert snapshot["rtp"] == [{"type": "inbound-rtp", "kind": "audio", "packetsReceived": 42, "jitter": 0}]
    assert "private-address" not in json.dumps(snapshot)
    assert await peer_snapshot(None) == {"active": False}


@pytest.mark.asyncio
async def test_ha_diagnostics_never_includes_config_entry_credentials():
    coord = SimpleNamespace(
        mqtt_connected=True, doorbell_state="idle", signal_trace=SignalTrace("client"),
        video=SimpleNamespace(_mon_pc1=None, _mon_pc2=None, _passive_pc=None,
                              live=SimpleNamespace(viewers=[])),
    )
    hass = SimpleNamespace(data={"aiphone": {"entry": coord}})
    entry = SimpleNamespace(entry_id="entry", data={"cert_pem": "private-cert", "seckey_pem": "private-key"})
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["media"]["monitor_publisher"] == {"active": False}
    assert result["media"]["viewers"] == {"audio": 0, "video": 0}
    assert "private-" not in json.dumps(result)
