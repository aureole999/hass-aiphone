"""Snapshots, status streams, audio readiness and foreground call policies."""
import asyncio
import io
import time
import types
from unittest.mock import AsyncMock

import av
import pytest
from PIL import Image

from aiphone_test.audio import UplinkAudioTrack
from aiphone_test.reply_files import resolve_reply_file, validate_reply_filename
from aiphone_test.snapshot import SnapshotCache
from aiphone_test.status_video import StatusAudioTrack, StatusVideoTrack
from test_calls import LocalCloud
from test_live import TestTone
from test_reply import has_signal


def test_bundled_reply_is_selected_without_a_filename(tmp_path):
    assert validate_reply_filename("  ") == ""
    with av.open(str(resolve_reply_file(tmp_path, ""))) as audio:
        assert audio.streams.audio
        assert 0 < sum(f.samples / f.sample_rate for f in audio.decode(audio=0)) < 30


@pytest.mark.asyncio
async def test_snapshot_placeholder_cache_and_restart_are_passive(tmp_path):
    cache = SnapshotCache(tmp_path, "unit:client")
    placeholder = await cache.picture()
    assert Image.open(io.BytesIO(placeholder)).format == "JPEG"
    assert not cache.path.exists()
    source = av.VideoFrame.from_image(Image.new("RGB", (64, 48), "red"))
    await cache.flush(source)
    assert cache.jpeg != placeholder
    assert cache.captured_at is not None
    restarted = SnapshotCache(tmp_path, "unit:client")
    assert await restarted.picture() == cache.jpeg
    assert abs(restarted.captured_at - cache.captured_at) < .01
    assert await SnapshotCache(tmp_path, "other-client").picture() == placeholder


@pytest.mark.asyncio
async def test_snapshot_write_failure_does_not_block_call_cleanup(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_bytes(b"x")
    cache = SnapshotCache(blocker, "client")
    await cache.flush(av.VideoFrame.from_image(Image.new("RGB", (64, 48))))
    assert cache.jpeg
    assert not cache.path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,ringing,expected", [
    ("off", False, False), ("off", True, False),
    ("ringing", False, False), ("ringing", True, True),
    ("always", False, True), ("always", True, True),
])
async def test_auto_answer_policy_does_not_claim_microphone(media_module, tmp_path, mode, ringing, expected):
    cloud = LocalCloud(media_module, tmp_path)
    cloud.entry = types.SimpleNamespace(options={"auto_answer": mode})
    cloud.active_call_id = "ring" if ringing else None
    cloud.doorbell_state = "ringing" if ringing else "idle"
    cloud.video._passive_cid = "ring" if ringing else None
    cloud.video._mon_cid = None if ringing else "monitor"
    lease = types.SimpleNamespace(stop=lambda: None)
    cloud.video.async_viewer_track = AsyncMock(return_value=lease)
    cloud.video.async_start_monitor_talk = AsyncMock(return_value=True)
    cloud.video.async_answer_ring = AsyncMock(return_value=True)
    await cloud.video.async_auto_answer(lambda: True)
    answer = cloud.video.async_answer_ring if ringing else cloud.video.async_start_monitor_talk
    assert answer.await_count == int(expected)
    assert cloud.video._talk_owner is None
    assert cloud.video._uplink is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["viewer_closed", "disabled", "call_changed"])
async def test_auto_answer_rechecks_after_media_setup(media_module, tmp_path, change):
    cloud = LocalCloud(media_module, tmp_path)
    cloud.entry = types.SimpleNamespace(options={"auto_answer": "always"})
    active = True
    async def opening():
        nonlocal active
        cloud.video._mon_cid = "monitor"
        if change == "viewer_closed":
            active = False
        elif change == "disabled":
            cloud.entry.options["auto_answer"] = "off"
        return types.SimpleNamespace(stop=lambda: None)
    cloud.video.async_viewer_track = opening
    async def answer(allowed):
        if change == "call_changed":
            cloud.video._mon_cid = "replacement"
        assert not allowed()
    cloud.video.async_start_monitor_talk = AsyncMock(side_effect=answer)
    await cloud.video.async_auto_answer(lambda: active)
    assert cloud.video.async_start_monitor_talk.await_count == int(change == "call_changed")


@pytest.mark.asyncio
async def test_reply_waits_for_post_accept_audio_and_both_connected_peers(media_module, tmp_path):
    cloud = LocalCloud(media_module, tmp_path)
    video = cloud.video
    video._mon_cid = "monitor"
    video._accepted = True
    video._accepted_at = time.monotonic()
    video._uplink = UplinkAudioTrack()
    video._uplink.enabled = True
    video._mon_pc1 = types.SimpleNamespace(connectionState="connected")
    video._mon_pc2 = types.SimpleNamespace(connectionState="connecting")
    video._audio_received_at = video._accepted_at - 1
    waiting = asyncio.create_task(video.wait_for_reply_audio("monitor", 2))
    try:
        await asyncio.sleep(.1)
        assert not waiting.done()
        video._mon_pc2.connectionState = "connected"
        await asyncio.sleep(.1)
        assert not waiting.done()  # Audio from before answer cannot open the gate.
        video._audio_received_at = time.monotonic()
        await asyncio.sleep(.1)
        assert not waiting.done()  # Allow the return audio path to settle.
        await waiting
        video._talk_owner = "browser"
        video._uplink.owner = "browser"
        video._uplink.last_frame_sent_at = time.monotonic()
        assert video.call_status == "in_call"
        video._audio_received_at -= 2
        assert video.call_status == "audio_interrupted"
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        video._uplink.stop()


@pytest.mark.asyncio
async def test_ended_ring_resets_requesting_status(media_module, tmp_path):
    cloud = LocalCloud(media_module, tmp_path)
    cloud.video._passive_ready.set()
    cloud.active_call_id = None
    with pytest.raises(RuntimeError, match="ended"):
        await cloud.video.async_answer_ring("ended")
    assert not cloud.video._answering


@pytest.mark.asyncio
async def test_status_video_survives_cached_live_cached_transition(media_module, tmp_path):
    cloud = LocalCloud(media_module, tmp_path)
    cloud.video.async_viewer_track = AsyncMock(return_value=types.SimpleNamespace(stop=lambda: None))
    track = StatusVideoTrack(cloud.video)
    try:
        first = await track.recv()
        assert (first.width, first.height) == (640, 480)
        assert cloud.video.live.external_viewers == 1
        cloud.video._mon_cid = "monitor"
        cloud.video._latest_video_frame = av.VideoFrame.from_image(Image.new("RGB", (64, 48), "red"))
        cloud.video._video_received_at = time.monotonic()
        live = await track.recv()
        assert live.to_image().getpixel((320, 240))[0] > 200
        cloud.video._latest_video_frame = None
        cloud.video._mon_cid = None
        cached = await track.recv()
        assert first.pts < live.pts < cached.pts
        assert cached.to_image().getpixel((320, 240)) != live.to_image().getpixel((320, 240))
    finally:
        track.stop()
        await track._opening
    assert cloud.video.live.external_viewers == 0


@pytest.mark.asyncio
async def test_status_audio_survives_silent_live_silent_transition(media_module, tmp_path):
    cloud = LocalCloud(media_module, tmp_path)
    track, source = StatusAudioTrack(cloud.video), TestTone()
    try:
        initial = await track.recv()
        assert not has_signal(initial)
        cloud.video._latest_audio_frame = await source.recv()
        cloud.video._audio_frames += 1
        cloud.video._audio_received_at = time.monotonic()
        live = await track.recv()
        assert has_signal(live)
        cloud.video._latest_audio_frame = None
        final = await track.recv()
        assert not has_signal(final)
        assert initial.pts < live.pts < final.pts
    finally:
        track.stop()
        source.stop()


@pytest.mark.asyncio
async def test_paused_microphone_is_discarded_when_original_viewer_closes():
    uplink, microphone, recording = UplinkAudioTrack(), TestTone(), TestTone()
    uplink.enabled = True
    try:
        uplink.claim("browser", microphone)
        uplink.pause()
        uplink.claim("reply", recording)
        uplink.release("browser")
        assert uplink.owner == "reply"
        assert microphone.readyState == "ended"
        uplink.release("reply")
        assert not uplink.resume()
        assert uplink.owner is None
    finally:
        uplink.stop()
