"""Real aiortc audio tests and call lifecycle checks; no Aiphone/cloud traffic."""

import asyncio
import math
import struct
import wave

import pytest

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



@pytest.fixture
def cloud(media_module, tmp_path):
    from test_calls import LocalCloud
    return LocalCloud(media_module, tmp_path)


@pytest.mark.asyncio
async def test_real_local_reply_reuses_call_publisher_and_releases(cloud, audio_file):
    try:
        await cloud.ring()
        await cloud.reply.play(audio_file.parent, audio_file.name)
        assert cloud.audible.is_set()
        assert cloud.reply.status == "sent"
        assert not cloud.reply.busy
        mids = [m["HEADER"]["MID"] for m in cloud.messages]
        assert mids == ["30011", "30012", "24001", "30021", "24002"]
        assert cloud.video._ring_publisher is not None
        assert cloud.video._accepted
    finally:
        await cloud.close()


@pytest.mark.asyncio
async def test_missing_file_cannot_accept_a_call(cloud, tmp_path):
    try:
        with pytest.raises(reply.ReplyError, match="Put the selected"):
            await cloud.reply.play(tmp_path, "missing.wav")
        assert cloud.messages == []
        assert cloud.reply.status == "failed"
        assert not cloud.reply.busy
    finally:
        await cloud.close()


@pytest.mark.asyncio
async def test_idle_reply_starts_monitor_and_keeps_call_connected(cloud, audio_file):
    cloud.doorbell_state, cloud.active_call_id = "idle", None
    try:
        await cloud.reply.play(audio_file.parent, audio_file.name)
        assert cloud.audible.is_set()
        assert cloud.video._monitor_talking
        assert cloud.video._uplink.owner is None
        assert "23003" not in [m["HEADER"]["MID"] for m in cloud.messages]
    finally:
        await cloud.close()


@pytest.mark.asyncio
async def test_duplicate_press_and_remote_end_cancel_without_playing(cloud, audio_file):
    await cloud.ring()
    cloud.ignore.add("30021")
    task = asyncio.create_task(cloud.reply.play(audio_file.parent, audio_file.name))
    try:
        async with asyncio.timeout(3):
            while not any(m["HEADER"]["MID"] == "30021" for m in cloud.messages):
                await asyncio.sleep(.01)
        with pytest.raises(reply.ReplyError, match="already"):
            await cloud.reply.play(audio_file.parent, audio_file.name)
        cloud.reply.on_signal("23003", {"HEADER": {"CID": "ring-1"}})
        with pytest.raises(reply.ReplyError, match="stopped"):
            await task
        assert cloud.reply.status == "stopped"
        assert not cloud.audible.is_set()
    finally:
        await cloud.close()


@pytest.mark.asyncio
async def test_other_client_answer_cancels_reply(cloud, audio_file):
    await cloud.ring()
    cloud.ignore.add("30021")
    task = asyncio.create_task(cloud.reply.play(audio_file.parent, audio_file.name))
    try:
        await asyncio.sleep(.1)
        cloud.reply.on_signal("24002", {"HEADER": {
            "CID":"ring-1", "CLIID":"official-app", "RSLT":200,
        }})
        with pytest.raises(reply.ReplyError, match="stopped"):
            await task
        assert not cloud.reply.busy
        assert not cloud.audible.is_set()
    finally:
        await cloud.close()


@pytest.mark.asyncio
async def test_corrupt_audio_cannot_accept_a_call(cloud, tmp_path):
    (tmp_path / "broken.wav").write_bytes(b"not a wave file")
    try:
        with pytest.raises(reply.ReplyError):
            await cloud.reply.play(tmp_path, "broken.wav")
        assert cloud.messages == []
        assert not cloud.reply.busy
    finally:
        await cloud.close()


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
async def test_explicit_stop_closes_real_sender(cloud, audio_file):
    await cloud.ring()
    task = asyncio.create_task(cloud.reply.play(audio_file.parent, audio_file.name))
    try:
        async with asyncio.timeout(5):
            while cloud.reply.status != "sending":
                await asyncio.sleep(.01)
        await cloud.reply.stop()
        with pytest.raises(reply.ReplyError, match="stopped"):
            await task
        assert cloud.video._ring_publisher is not None
        assert cloud.video._accepted
        assert not cloud.reply.busy
    finally:
        await cloud.close()


@pytest.mark.asyncio
async def test_reply_takes_over_and_restores_live_microphone(cloud, audio_file):
    from test_live import TestTone
    microphone = TestTone()
    try:
        await cloud.ring()
        await cloud.video.async_microphone(microphone, "talking-browser")
        await cloud.reply.play(audio_file.parent, audio_file.name)
        assert cloud.reply.status == "sent"
        assert cloud.video._uplink.owner == "talking-browser"
        assert cloud.video._talk_owner == "talking-browser"
        assert microphone.readyState == "live"
        assert cloud.video._accepted
    finally:
        await cloud.close()


@pytest.mark.asyncio
async def test_monitor_reply_reuses_publisher_without_reanswering(cloud, audio_file):
    try:
        await cloud.monitor()
        await cloud.video.async_start_monitor_talk()
        publisher = cloud.video._mon_pc1
        await cloud.reply.play(audio_file.parent, audio_file.name)
        mids = [m["HEADER"]["MID"] for m in cloud.messages]
        assert mids.count("23022") == 1
        assert mids.count("30001") == 1
        assert cloud.video._mon_pc1 is publisher
        assert cloud.reply.status == "sent"
        assert cloud.video._accepted
    finally:
        await cloud.close()


@pytest.mark.asyncio
async def test_closing_original_microphone_during_reply_keeps_reply_and_does_not_restore_mic(cloud, audio_file):
    from test_live import TestTone
    microphone = TestTone()
    try:
        await cloud.ring()
        await cloud.video.async_microphone(microphone, "browser")
        task = asyncio.create_task(cloud.reply.play(audio_file.parent, audio_file.name))
        async with asyncio.timeout(5):
            while cloud.reply.status != "sending":
                await asyncio.sleep(.01)
        await cloud.video.async_microphone_closed("browser")
        await task
        assert microphone.readyState == "ended"
        assert cloud.video._talk_owner is None
        assert cloud.video._uplink.owner is None
        assert cloud.video._accepted
        assert cloud.reply.status == "sent"
    finally:
        await cloud.close()
