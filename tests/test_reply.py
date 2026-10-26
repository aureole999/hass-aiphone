"""Real aiortc audio tests and call lifecycle checks; no Aiphone/cloud traffic."""

import asyncio
import math
import struct
import wave

import pytest
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

from aiphone_test import reply
from aiphone_test.reply_files import resolve_reply_file, validate_reply_filename


def has_signal(frame):
    assert frame.format.name == "s16"
    count = frame.samples * len(frame.layout.channels)
    data = bytes(frame.planes[0])[: count * 2]
    return max(abs(s) for s in struct.unpack("<" + "h" * count, data)) > 500


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "reply.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(
            b"".join(
                struct.pack("<h", int(12000 * math.sin(i * 0.2))) for i in range(6400)
            )
        )
    return path


@pytest.mark.parametrize(
    "name",
    [
        "../secret.wav",
        "/tmp/a.wav",
        "https://a/b.wav",
        "a\\b.wav",
        "private.pem",
        "",
        ".hidden.wav",
    ],
)
def test_reject_nonlocal_audio_paths(name):
    with pytest.raises(ValueError):
        validate_reply_filename(name)


def test_reject_symlink_outside_audio_directory(tmp_path, audio_file):
    directory = tmp_path / "replies"
    directory.mkdir()
    (directory / "alias.wav").symlink_to(audio_file)
    with pytest.raises(ValueError, match="inside"):
        resolve_reply_file(directory, "alias.wav")


@pytest.mark.asyncio
async def test_audio_is_silent_until_started_and_paced_once(audio_file):
    player = reply._open_audio(audio_file.parent, audio_file.name)
    track = reply.GatedAudioTrack(player.audio)
    try:
        for _ in range(3):
            frame = await track.recv()
            assert not has_signal(frame)
        assert track.file_samples == 0
        track.begin()
        started = asyncio.get_running_loop().time()
        loud = []
        timestamps = []
        while not track.finished.is_set():
            frame = await track.recv()
            loud.append(has_signal(frame))
            timestamps.append(frame.pts)
        assert any(loud)
        assert track.file_samples == 19200  # resampled 0.4 s, not pre-consumed
        assert asyncio.get_running_loop().time() - started >= 0.35
        assert timestamps == sorted(set(timestamps))
        assert not has_signal(await track.recv())
    finally:
        track.stop()
        await asyncio.to_thread(reply._close_player, player)


class FakeCoordinator:
    cliid = "our-client"
    rcvtpc = "unit/receive"
    sndtpc = "unit/send/device"

    def __init__(self):
        self.active_call_id = "current-ring"
        self.doorbell_state = "ringing"
        self.messages = []
        self.publisher = reply.ReplyPublisher(self)
        self.peer = None
        self.background = []
        self.received_nonzero = False
        self.on_accept = None

    def notify_reply_state(self):
        pass

    def publish(self, message):
        self.messages.append(message)
        mid = message["HEADER"]["MID"]
        if mid == "30001":
            self.background.append(asyncio.create_task(self.answer_offer(message)))
        elif mid == "24000":
            self.doorbell_state = "answered"
            self.publisher.on_signal(mid, message)
            if self.on_accept:
                self.on_accept()
        return True

    async def answer_offer(self, message):
        self.peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))

        @self.peer.on("track")
        def on_track(track):
            self.background.append(asyncio.create_task(self.receive_audio(track)))

        await self.peer.setRemoteDescription(
            RTCSessionDescription(message["BODY"]["SDP"], "offer")
        )
        await self.peer.setLocalDescription(await self.peer.createAnswer())
        self.publisher.on_signal(
            "31001",
            {
                "HEADER": {"MID": "31001", "CID": self.active_call_id, "RSLT": 0},
                "BODY": {"SDP": self.peer.localDescription.sdp},
            },
        )

    async def receive_audio(self, track):
        try:
            while True:
                frame = await track.recv()
                self.received_nonzero |= has_signal(frame)
        except MediaStreamError:
            pass

    async def close(self):
        await self.publisher.stop()
        if self.peer:
            await self.peer.close()
        for task in self.background:
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_local_webrtc_publisher_sends_file_and_releases(audio_file):
    coord = FakeCoordinator()
    try:
        await coord.publisher.play(audio_file.parent, audio_file.name)
        assert coord.received_nonzero
        assert coord.publisher.status == "sent"
        assert not coord.publisher.busy
        assert [m["HEADER"]["MID"] for m in coord.messages] == ["30001", "24000"]
        assert all(m["HEADER"]["CID"] == "current-ring" for m in coord.messages)
        assert coord.peer.connectionState == "closed"
    finally:
        await coord.close()


@pytest.mark.asyncio
async def test_missing_file_cannot_accept_a_call(tmp_path):
    coord = FakeCoordinator()
    with pytest.raises(reply.ReplyError, match="Put the selected"):
        await coord.publisher.play(tmp_path, "missing.wav")
    assert coord.messages == []
    assert coord.publisher.status == "failed"
    assert not coord.publisher.busy


@pytest.mark.asyncio
async def test_no_ringing_call_cannot_send_any_command(audio_file):
    coord = FakeCoordinator()
    coord.doorbell_state = "idle"
    with pytest.raises(reply.ReplyError, match="only while"):
        await coord.publisher.play(audio_file.parent, audio_file.name)
    assert coord.messages == []


@pytest.mark.asyncio
async def test_duplicate_press_and_call_end_cancel_without_accepting(audio_file):
    coord = FakeCoordinator()
    coord.publish = lambda msg: coord.messages.append(msg) or True
    task = asyncio.create_task(coord.publisher.play(audio_file.parent, audio_file.name))
    try:
        for _ in range(100):
            if coord.messages:
                break
            await asyncio.sleep(0.01)
        assert coord.messages
        with pytest.raises(reply.ReplyError, match="already"):
            await coord.publisher.play(audio_file.parent, audio_file.name)
        coord.publisher.on_signal("24002", {"HEADER": {"CID": "current-ring"}})
        with pytest.raises(reply.ReplyError, match="stopped"):
            await task
        assert coord.publisher.status == "stopped"
        assert not coord.publisher.busy
        assert all(m["HEADER"]["MID"] != "24000" for m in coord.messages)
    finally:
        await coord.close()


@pytest.mark.asyncio
async def test_other_client_answer_cancels_during_negotiation(audio_file):
    coord = FakeCoordinator()
    coord.publish = lambda msg: coord.messages.append(msg) or True
    task = asyncio.create_task(coord.publisher.play(audio_file.parent, audio_file.name))
    try:
        for _ in range(100):
            if coord.messages:
                break
            await asyncio.sleep(0.01)
        coord.publisher.on_signal(
            "24000",
            {
                "HEADER": {
                    "CID": "current-ring",
                    "CLIID": "official-phone",
                    "RSLT": 200,
                }
            },
        )
        with pytest.raises(reply.ReplyError, match="stopped"):
            await task
        assert not coord.publisher.busy
    finally:
        await coord.close()


@pytest.mark.asyncio
async def test_corrupt_audio_cannot_accept_a_call(tmp_path):
    (tmp_path / "broken.wav").write_bytes(b"not a wave file")
    coord = FakeCoordinator()
    with pytest.raises(reply.ReplyError):
        await coord.publisher.play(tmp_path, "broken.wav")
    assert coord.messages == []
    assert not coord.publisher.busy


def test_long_audio_is_rejected_before_publishing(tmp_path):
    path = tmp_path / "long.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(bytes(31 * 8000 * 2))
    with pytest.raises(ValueError, match="30 seconds"):
        reply._open_audio(tmp_path, path.name)


@pytest.mark.asyncio
async def test_wrong_cid_cannot_complete_negotiation(audio_file, monkeypatch):
    coord = FakeCoordinator()
    monkeypatch.setattr(reply, "SIGNAL_TIMEOUT", 0.1)

    def wrong_reply(message):
        coord.messages.append(message)
        coord.publisher.on_signal(
            "31001",
            {
                "HEADER": {"CID": "another-call"},
                "BODY": {"SDP": "not our answer"},
            },
        )
        return True

    coord.publish = wrong_reply
    with pytest.raises(reply.ReplyError, match="Timed out"):
        await coord.publisher.play(audio_file.parent, audio_file.name)
    assert [m["HEADER"]["MID"] for m in coord.messages] == ["30001"]
    assert not coord.publisher.busy


@pytest.mark.asyncio
async def test_explicit_stop_closes_real_sender(audio_file):
    coord = FakeCoordinator()
    task = asyncio.create_task(coord.publisher.play(audio_file.parent, audio_file.name))
    try:
        for _ in range(300):
            if coord.publisher.status == "sending":
                break
            await asyncio.sleep(0.01)
        assert coord.publisher.status == "sending"
        await coord.publisher.stop()
        with pytest.raises(reply.ReplyError, match="stopped"):
            await task
        assert not coord.publisher.busy
        assert coord.publisher.status == "stopped"
        assert coord.peer.connectionState == "closed"
    finally:
        await coord.close()
