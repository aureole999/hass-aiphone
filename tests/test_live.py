"""Real local WebRTC video and shared recorder tests; never contact a doorbell."""

import asyncio
import importlib.util
import sys
import types
from enum import IntFlag
from pathlib import Path

import av
import pytest
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaRecorder
from aiortc.mediastreams import MediaStreamError
from webrtc_models import RTCIceCandidateInit

from aiphone_test.live import BrowserVideo, LiveVideoRelay


class TestPattern(VideoStreamTrack):
    __test__ = False

    def __init__(self):
        super().__init__()
        self.reading = False
        self.frames = 0

    async def recv(self):
        assert not self.reading, "Multiple consumers are reading the upstream track"
        self.reading = True
        try:
            pts, time_base = await self.next_timestamp()
            if self.readyState != "live":
                raise MediaStreamError
            frame = av.VideoFrame(64, 48, "yuv420p")
            for plane, value in zip(frame.planes, (150, 110, 180)):
                plane.update(bytes([value]) * plane.buffer_size)
            frame.pts, frame.time_base = pts, time_base
            self.frames += 1
            return frame
        finally:
            self.reading = False


async def open_browser(bridge, name):
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    browser.addTransceiver("video", direction="recvonly")
    browser.addTransceiver("audio", direction="recvonly")
    video = asyncio.get_running_loop().create_future()

    @browser.on("track")
    def on_track(track):
        if track.kind == "video":
            video.set_result(track)

    try:
        await browser.setLocalDescription(await browser.createOffer())
        answers = []
        await bridge.offer(browser.localDescription.sdp, name, answers.append)
        assert len(answers) == 1
        assert "a=inactive" in answers[0].split("m=audio", 1)[1]
        await browser.setRemoteDescription(RTCSessionDescription(answers[0], "answer"))
        return browser, await asyncio.wait_for(video, 5)
    except BaseException:
        await browser.close()
        raise


@pytest.mark.asyncio
async def test_two_viewers_and_recording_share_real_video(tmp_path):
    source = TestPattern()
    relay = LiveVideoRelay()
    recording = tmp_path / "shared.mp4"
    recorder = MediaRecorder(str(recording))
    recorder.addTrack(relay.attach(source))
    await recorder.start()
    bridge = BrowserVideo(relay.subscribe)
    browsers = []
    try:
        first, second = await asyncio.gather(open_browser(bridge, "one"), open_browser(bridge, "two"))
        browsers = [first[0], second[0]]
        for _, track in (first, second):
            a = await asyncio.wait_for(track.recv(), 10)
            b = await asyncio.wait_for(track.recv(), 3)
            assert b.pts > a.pts
            assert sum(bytes(b.planes[0])[:b.width]) / b.width > 90
        assert len(relay.viewers) == 2

        await bridge.close("one")
        assert len(relay.viewers) == 1
        assert source.readyState == "live"
        await asyncio.wait_for(second[1].recv(), 3)
        relay.end()
        for _ in range(20):
            if not bridge._sessions:
                break
            await asyncio.sleep(0.01)
        assert not bridge._sessions
    finally:
        await bridge.aclose()
        for browser in browsers:
            await browser.close()
        await recorder.stop()
        source.stop()
        relay.end()
        await asyncio.sleep(0.05)
    with av.open(str(recording)) as container:
        frames = list(container.decode(video=0))
    assert len(frames) >= 2
    assert frames[-1].pts > frames[0].pts


@pytest.mark.asyncio
async def test_browser_close_cancels_pending_video_start():
    waiting = asyncio.Event()

    async def get_track():
        waiting.set()
        await asyncio.Future()

    bridge = BrowserVideo(get_track)
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    browser.addTransceiver("video", direction="recvonly")
    try:
        await browser.setLocalDescription(await browser.createOffer())
        answers = []
        offer = asyncio.create_task(bridge.offer(browser.localDescription.sdp, "pending", answers.append))
        await asyncio.wait_for(waiting.wait(), 3)
        await bridge.close("pending")
        await asyncio.wait_for(offer, 3)
        assert not answers
        assert not bridge._sessions
    finally:
        await bridge.aclose()
        await browser.close()


@pytest.mark.asyncio
async def test_setup_failure_and_ice_candidates_are_cleaned_up():
    async def fail():
        raise TimeoutError("No camera video")

    bridge = BrowserVideo(fail)
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    browser.addTransceiver("video", direction="recvonly")
    try:
        await browser.setLocalDescription(await browser.createOffer())
        # Models are the same types Home Assistant passes to camera entities.
        candidate = RTCIceCandidateInit(
            "candidate:1 1 UDP 2130706431 127.0.0.1 12345 typ host",
            sdp_mid="0", sdp_m_line_index=0,
        )
        offer = asyncio.create_task(bridge.offer(browser.localDescription.sdp, "failed", lambda _: None))
        await asyncio.sleep(0)
        await bridge.candidate("failed", candidate)
        with pytest.raises(TimeoutError, match="No camera video"):
            await offer
        assert not bridge._sessions
        await bridge.candidate("failed", RTCIceCandidateInit(""))
    finally:
        await bridge.aclose()
        await browser.close()


@pytest.mark.asyncio
async def test_monitor_stays_open_until_last_viewer_leaves():
    relay = LiveVideoRelay()
    source = TestPattern()
    recorder_track = relay.attach(source)
    viewer = await relay.subscribe()
    hold = asyncio.create_task(relay.hold_monitor(0))
    try:
        await asyncio.sleep(0)
        assert not hold.done()
        viewer.stop()
        await asyncio.wait_for(hold, 1)
    finally:
        recorder_track.stop()
        source.stop()
        relay.end()


@pytest.fixture
def media_module(monkeypatch):
    """Stub only HA host plumbing and the auto-install shim, not the media code."""
    deps = types.ModuleType("aiphone_test._deps")
    deps.ensure_runtime_deps = lambda: None
    dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    dispatcher.async_dispatcher_send = lambda *args: None
    monkeypatch.setitem(sys.modules, deps.__name__, deps)
    monkeypatch.setitem(sys.modules, dispatcher.__name__, dispatcher)
    path = Path(__file__).parents[1] / "custom_components/aiphone/media.py"
    spec = importlib.util.spec_from_file_location("aiphone_test.media", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_multiple_camera_opens_start_one_monitor_and_unload_releases_lock(media_module):
    coord = types.SimpleNamespace(
        mqtt_connected=True, hass=types.SimpleNamespace(async_create_task=asyncio.create_task)
    )
    video = media_module.VideoCapture(coord)
    source = TestPattern()
    starts = []

    async def monitor_run(duration):
        starts.append(duration)
        video.live.attach(source)
        await asyncio.Future()

    video._monitor_run = monitor_run
    tracks = await asyncio.gather(video.async_viewer_track(), video.async_viewer_track())
    assert len(starts) == 1
    assert len(video.live.viewers) == 2
    await video.aclose()
    assert not video._lock.locked()
    assert all(track.readyState == "ended" for track in tracks)
    source.stop()


@pytest.mark.asyncio
async def test_call_end_releases_capture_lock_immediately_after_flush(media_module):
    video = media_module.VideoCapture(types.SimpleNamespace())
    ready = asyncio.Event()

    class Peer:
        async def getStats(self):
            return {}

        async def close(self):
            pass

    async def passive_run(cid, caller):
        video._passive_pc = Peer()
        ready.set()
        await video._passive_ended.wait()

    video._passive_run = passive_run
    task = asyncio.create_task(video.passive_capture("call", "Entrance"))
    try:
        await asyncio.wait_for(ready.wait(), 1)
        assert video._lock.locked()
        await video.passive_finalize(delay_s=0)
        await asyncio.wait_for(task, 1)
        assert not video._lock.locked()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_camera_declares_native_stream_and_keeps_existing_identity(monkeypatch):
    """Check the real entity's HA API wiring, with host dependencies substituted."""
    class Feature(IntFlag):
        STREAM = 2

    class Camera:
        pass

    camera_api = types.ModuleType("homeassistant.components.camera")
    camera_api.Camera = Camera
    camera_api.CameraEntityFeature = Feature
    camera_api.WebRTCAnswer = lambda sdp: sdp
    camera_api.WebRTCError = lambda *args: args
    camera_api.WebRTCSendMessage = object
    config = types.ModuleType("homeassistant.config_entries")
    config.ConfigEntry = object
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda function: function
    device = types.ModuleType("homeassistant.helpers.device_registry")
    device.CONNECTION_NETWORK_MAC = "mac"
    device.DeviceInfo = dict
    dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    dispatcher.async_dispatcher_connect = lambda *args: None
    platform = types.ModuleType("homeassistant.helpers.entity_platform")
    platform.AddEntitiesCallback = object
    coord = types.ModuleType("aiphone_test.coordinator")
    coord.AiphoneCoordinator = object
    for module in (camera_api, config, core, device, dispatcher, platform, coord):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    path = Path(__file__).parents[1] / "custom_components/aiphone/camera.py"
    spec = importlib.util.spec_from_file_location("aiphone_test.camera", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    relay = LiveVideoRelay()
    coordinator = types.SimpleNamespace(
        device_id="existing-id", device_mac="test-mac",
        video=types.SimpleNamespace(async_viewer_track=relay.subscribe),
    )
    entity = module.AiphoneEntranceCamera(coordinator)
    assert entity._attr_supported_features & Feature.STREAM
    assert callable(entity.async_handle_async_webrtc_offer)
    assert callable(entity.async_on_webrtc_candidate)
    assert callable(entity.close_webrtc_session)
    assert entity._attr_unique_id == "existing-id_entrance"
