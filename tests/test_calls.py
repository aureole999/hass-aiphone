"""App-derived signaling with real local SRTP peers; no doorbell/cloud access."""
import asyncio
import types

import pytest
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

from aiphone_test.audio import UplinkAudioTrack
from aiphone_test.live import BrowserVideo
from aiphone_test.reply import ReplyPublisher
from test_live import TestPattern, TestTone
from test_reply import has_signal


class LocalCloud:
    """Answers the actual integration's messages using local aiortc transports."""
    cliid = "local-client"
    sndtpc = "unit/send/indoor"
    rcvtpc = "unit/receive/client"
    termname = "Test"
    mqtt_connected = True
    call_kind = 1

    def __init__(self, module, directory):
        self.active_call_id = "ring-1"
        self.doorbell_state = "ringing"
        self.messages = []
        self.peers = []
        self.tasks = []
        self.packets = []
        self.audible = asyncio.Event()
        self.reject = {}
        self.ignore = set()
        self.hass = types.SimpleNamespace(
            config=types.SimpleNamespace(path=lambda sub: str(directory / sub)),
            async_create_task=asyncio.create_task,
            async_add_executor_job=asyncio.to_thread,
        )
        self.video = module.VideoCapture(self)
        self.reply = ReplyPublisher(self)

    def notify_reply_state(self):
        pass

    def call_answered(self):
        self.doorbell_state = "answered"

    def call_ended(self, cid=None):
        if cid and self.active_call_id and cid != self.active_call_id:
            return
        self.doorbell_state = "idle"
        self.active_call_id = None

    async def async_answer_call(self):
        if self.video._mon_cid:
            return await self.video.async_start_monitor_talk()
        return await self.video.async_answer_ring(self.active_call_id)

    def respond(self, mid, cid, body=None, code=200):
        message = {"HEADER": {"MID": mid, "CID": cid, "RSLT": code}, "BODY": body or {}}
        self.video.on_signal(mid, message)
        self.reply.on_signal(mid, message)

    def publish(self, message):
        self.messages.append(message)
        mid, cid = message["HEADER"]["MID"], message["HEADER"]["CID"]
        if mid in self.ignore:
            return True
        if mid in ("30001", "30021"):
            self.tasks.append(asyncio.create_task(self.publisher(message)))
        elif mid == "30011":
            self.tasks.append(asyncio.create_task(self.subscriber(cid)))
        elif mid == "30012":
            self.tasks.append(asyncio.create_task(self.subscriber_answer(message)))
        else:
            reply = {"23021":"24021", "23022":"24022", "24002":"25001",
                     "30031":"31031", "23003":"24003"}.get(mid)
            if reply:
                self.respond(reply, cid, code=self.reject.get(mid, 200))
        return True

    async def publisher(self, message):
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.peers.append(pc)
        await pc.setRemoteDescription(RTCSessionDescription(message["BODY"]["SDP"], "offer"))
        receiver = pc.getReceivers()[0]
        handle = receiver._handle_rtp_packet

        async def inspect(packet, arrival_time_ms):
            self.packets.append((packet.payload_type, packet.timestamp, len(packet.payload)))
            await handle(packet, arrival_time_ms)

        receiver._handle_rtp_packet = inspect
        self.tasks.append(asyncio.create_task(self.drain(receiver.track)))
        await pc.setLocalDescription(await pc.createAnswer())
        mid = "31001" if message["HEADER"]["MID"] == "30001" else "31021"
        self.respond(mid, message["HEADER"]["CID"], {"SDP": pc.localDescription.sdp}, 0)

    async def drain(self, track):
        try:
            while True:
                if has_signal(await track.recv()):
                    self.audible.set()
        except MediaStreamError:
            pass

    async def subscriber(self, cid):
        pc = self.subscriber_pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.peers.append(pc)
        pc.addTrack(TestTone())
        pc.addTrack(TestPattern())
        await pc.setLocalDescription(await pc.createOffer())
        self.respond("31011", cid, {"SDP": pc.localDescription.sdp}, 0)

    async def subscriber_answer(self, message):
        await self.subscriber_pc.setRemoteDescription(RTCSessionDescription(message["BODY"]["SDP"], "answer"))
        self.respond("31012", message["HEADER"]["CID"])

    async def ring(self):
        self.capture = asyncio.create_task(self.video.passive_capture(self.active_call_id, "Entrance"))
        await asyncio.wait_for(self.video._passive_ready.wait(), 5)

    async def monitor(self):
        self.active_call_id, self.doorbell_state = None, "idle"
        self.capture = asyncio.create_task(self.video.monitor_capture(60))
        await asyncio.wait_for(self.video.live.ready.wait(), 5)

    async def close(self):
        await self.reply.stop()
        await self.video.aclose()
        for pc in self.peers:
            for sender in pc.getSenders():
                if sender.track:
                    sender.track.stop()
            await pc.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_incoming_uses_app_handshake_and_exact_pcmu_packets(media_module, tmp_path):
    cloud = LocalCloud(media_module, tmp_path)
    source = TestTone()
    try:
        await cloud.ring()
        assert not cloud.audible.is_set()
        await cloud.video.async_microphone(source, "browser")
        await asyncio.wait_for(cloud.audible.wait(), 5)
        mids = [m["HEADER"]["MID"] for m in cloud.messages]
        assert mids[:5] == ["30011", "30012", "24001", "30021", "24002"]
        assert "30001" not in mids and "24000" not in mids
        assert cloud.video._accepted and cloud.doorbell_state == "answered"
        assert all(pt == 0 and size == 160 for pt, _, size in cloud.packets)
        timestamps = [ts for _, ts, _ in cloud.packets]
        assert len(timestamps) > 3
        assert all((b-a) % (2**32) == 160 for a,b in zip(timestamps, timestamps[1:]))
        await cloud.video.async_microphone_closed("browser")
        assert [m["HEADER"]["MID"] for m in cloud.messages][-2:] == ["30031", "23003"]
        assert not cloud.video._lock.locked()
        assert cloud.video._ring_publisher is None
        assert source.readyState == "ended"
        assert cloud.doorbell_state == "idle"
    finally:
        await cloud.close()


@pytest.mark.parametrize("kind", ["monitor", "ring"])
@pytest.mark.asyncio
async def test_monitor_and_ring_bridge_real_bidirectional_audio(media_module, tmp_path, kind):
    cloud = LocalCloud(media_module, tmp_path)
    browser = None
    try:
        await (cloud.monitor() if kind == "monitor" else cloud.ring())
        bridge = BrowserVideo(cloud.video.async_viewer_track, cloud.video.viewer_audio_track,
                              cloud.video.async_microphone, cloud.video.async_microphone_closed)
        browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        browser.addTransceiver("video", direction="recvonly")
        browser.addTrack(TestTone())
        await browser.setLocalDescription(await browser.createOffer())
        answers = []
        await bridge.offer(browser.localDescription.sdp, "talk", answers.append)
        assert "a=sendrecv" in answers[0].split("m=audio",1)[1]
        await browser.setRemoteDescription(RTCSessionDescription(answers[0], "answer"))
        downlink = next(r.track for r in browser.getReceivers() if r.track.kind == "audio")
        assert has_signal(await asyncio.wait_for(downlink.recv(), 5))
        await asyncio.wait_for(cloud.audible.wait(), 5)
        assert cloud.video._accepted
        assert [m["HEADER"]["MID"] for m in cloud.messages].count("23022" if kind == "monitor" else "24002") == 1
        # A second microphone cannot take over; closing an unrelated viewer
        # cannot disconnect the microphone owner.
        other = TestTone()
        with pytest.raises(RuntimeError, match="Another microphone"):
            await cloud.video.async_microphone(other, "other")
        other.stop()
        await cloud.video.async_microphone_closed("other")
        assert cloud.video._accepted
        await bridge.close("talk")
        assert cloud.video.session_cid is None
        assert not cloud.video._lock.locked()
    finally:
        if browser:
            for sender in browser.getSenders():
                if sender.track:
                    sender.track.stop()
            await browser.close()
        await cloud.close()


@pytest.mark.asyncio
async def test_rejected_acceptance_never_opens_voice_gate(media_module, tmp_path):
    cloud = LocalCloud(media_module, tmp_path)
    cloud.reject["24002"] = 486
    source = TestTone()
    try:
        await cloud.ring()
        with pytest.raises(RuntimeError, match="rejected"):
            await cloud.video.async_microphone(source, "browser")
        assert not cloud.audible.is_set()
        assert not cloud.video._accepted
        assert cloud.video._uplink is None
        assert cloud.video._talk_owner is None
    finally:
        source.stop()
        await cloud.close()


@pytest.mark.asyncio
async def test_wrong_session_response_cannot_accept_call(media_module, tmp_path):
    cloud = LocalCloud(media_module, tmp_path)
    cloud.video._passive_cid = "right"
    cloud.ignore.add("24002")
    task = asyncio.create_task(cloud.video._request("24002", "25001", "right", timeout=.05))
    try:
        await asyncio.sleep(0)
        cloud.respond("25001", "wrong")
        with pytest.raises(RuntimeError, match="Timed out"):
            await task
        assert not cloud.video._signals
    finally:
        cloud.video._passive_cid = None
        await cloud.close()


@pytest.mark.asyncio
async def test_remote_end_only_releases_our_relay_without_hanging_up_other_app(media_module, tmp_path):
    cloud = LocalCloud(media_module, tmp_path)
    try:
        await cloud.ring()
        await cloud.video.async_hang_up(remote=True)
        mids = [m["HEADER"]["MID"] for m in cloud.messages]
        assert "30031" in mids
        assert "23003" not in mids
        assert not cloud.video._lock.locked()
    finally:
        await cloud.close()


@pytest.mark.asyncio
async def test_microphone_queue_is_bounded_and_gate_is_silent():
    track, source = UplinkAudioTrack(), TestTone()
    track.claim("one", source)
    try:
        for _ in range(3):
            assert not has_signal(await track.recv())
        track.enabled = True
        await asyncio.sleep(.1)
        assert track._queue.qsize() <= 2
        assert has_signal(await track.recv())
        with pytest.raises(RuntimeError, match="Another"):
            track.claim("two", source)
        track.release("not-owner")
        assert track.owner == "one"
        track.release("one")
        assert not has_signal(await track.recv())
    finally:
        track.stop()


@pytest.mark.asyncio
async def test_simultaneous_answer_buttons_accept_only_once(media_module, tmp_path):
    cloud = LocalCloud(media_module, tmp_path)
    try:
        await cloud.ring()
        assert await asyncio.gather(cloud.video.async_answer_ring("ring-1"), cloud.video.async_answer_ring("ring-1")) == [True, True]
        mids = [m["HEADER"]["MID"] for m in cloud.messages]
        assert mids.count("30021") == mids.count("24002") == 1
    finally:
        await cloud.close()


@pytest.mark.asyncio
async def test_read_only_camera_offer_never_answers_or_captures_microphone(media_module, tmp_path):
    cloud = LocalCloud(media_module, tmp_path)
    browser = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    bridge = BrowserVideo(cloud.video.async_viewer_track, cloud.video.viewer_audio_track,
                          cloud.video.async_microphone, cloud.video.async_microphone_closed)
    try:
        await cloud.monitor()
        browser.addTransceiver("video", direction="recvonly")
        browser.addTransceiver("audio", direction="recvonly")
        await browser.setLocalDescription(await browser.createOffer())
        answers = []
        await bridge.offer(browser.localDescription.sdp, "viewer", answers.append)
        assert not cloud.video._accepted
        assert cloud.video._talk_owner is None
        assert "23022" not in [m["HEADER"]["MID"] for m in cloud.messages]
        await bridge.close("viewer")
        assert cloud.video.session_cid  # other viewers may still use Monitor
    finally:
        await bridge.aclose()
        await browser.close()
        await cloud.close()


@pytest.mark.asyncio
async def test_explicit_hangup_declines_unanswered_ring_without_opening_microphone(media_module, tmp_path):
    cloud = LocalCloud(media_module, tmp_path)
    try:
        await cloud.ring()
        await cloud.video.async_hang_up()
        answer = next(m for m in cloud.messages if m["HEADER"]["MID"] == "24002")
        assert answer["HEADER"]["RSLT"] == 603
        assert not any(m["HEADER"]["MID"] in ("30021", "23003") for m in cloud.messages)
        assert cloud.video.session_cid is None
    finally:
        await cloud.close()
