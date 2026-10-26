"""Experimental prerecorded reply publisher for an existing ringing call.

The 30001 publisher is based on the upstream monitor implementation. Its use
with a real incoming-call CID is NOT hardware-verified. No guessed hangup or
door-release commands are sent. Local WebRTC resources are always closed.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

from .reply_files import MAX_REPLY_SECONDS, resolve_reply_file

_LOGGER = logging.getLogger(__name__)
SIGNAL_TIMEOUT = 10
CONNECTION_TIMEOUT = 15


class ReplyError(Exception):
    """Reply could not be sent; audible playback is never implied."""


class BufferedFileTrack(MediaStreamTrack):
    """Bounded decoded audio; pacing is applied by GatedAudioTrack."""

    kind = "audio"

    def __init__(self, frames: list[av.AudioFrame]) -> None:
        super().__init__()
        self._frames = deque(frames)

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live" or not self._frames:
            raise MediaStreamError
        return self._frames.popleft()

    def stop(self) -> None:
        self._frames.clear()
        super().stop()


class PreparedAudio:
    def __init__(self, frames: list[av.AudioFrame]) -> None:
        self.audio = BufferedFileTrack(frames)


def _open_audio(directory: Path, filename: str) -> PreparedAudio:
    """Validate and open before accepting the visitor's call (executor only)."""
    path = resolve_reply_file(directory, filename)
    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError("The selected file has no audio track.")
        stream = container.streams.audio[0]
        if stream.duration is not None and stream.time_base is not None:
            if float(stream.duration * stream.time_base) > MAX_REPLY_SECONDS:
                raise ValueError("Reply audio must be at most 30 seconds.")
        resampler = av.AudioResampler(
            format="s16", layout="stereo", rate=48000, frame_size=960
        )
        frames: list[av.AudioFrame] = []
        samples = 0

        def append(decoded: list[av.AudioFrame]) -> None:
            nonlocal samples
            for frame in decoded:
                samples += frame.samples
                if samples > MAX_REPLY_SECONDS * 48000:
                    raise ValueError("Reply audio must be at most 30 seconds.")
                frames.append(frame)

        for frame in container.decode(stream):
            append(resampler.resample(frame))
        append(resampler.resample(None))  # flush the final resampler samples
        if not frames:
            raise ValueError("The selected file has no decodable audio frames.")
    return PreparedAudio(frames)


class GatedAudioTrack(MediaStreamTrack):
    """Send paced silence until permitted, then the file exactly once.

    The file decoder supplies 48 kHz s16 stereo audio. RTP codec selection
    and encoding belong to aiortc's negotiated sender, not the source file.
    """

    kind = "audio"

    def __init__(self, source: MediaStreamTrack) -> None:
        super().__init__()
        self.source = source
        self.playing = False
        self.finished = asyncio.Event()
        self.error: str | None = None
        self.file_samples = 0
        self._samples = 0
        self._start: float | None = None

    def begin(self) -> None:
        self.playing = True

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        loop = asyncio.get_running_loop()
        if self._start is None:
            self._start = loop.time()
        await asyncio.sleep(max(0, self._start + self._samples / 48000 - loop.time()))
        frame = None
        if self.playing and not self.finished.is_set():
            try:
                frame = await self.source.recv()
                if frame.sample_rate != 48000:
                    raise ValueError("Unexpected decoded sample rate.")
                self.file_samples += frame.samples
                if self.file_samples > MAX_REPLY_SECONDS * 48000:
                    raise ValueError("Reply exceeded the 30-second limit.")
            except MediaStreamError:
                self.finished.set()
            except Exception:
                self.error = "Could not decode the reply audio."
                self.finished.set()
                frame = None
        if frame is None:
            frame = av.AudioFrame(format="s16", layout="stereo", samples=960)
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            frame.sample_rate = 48000
        frame.pts = self._samples
        frame.time_base = Fraction(1, 48000)
        self._samples += frame.samples
        return frame


class ReplyPublisher:
    """One explicitly requested reply, bound to one incoming-call CID."""

    def __init__(self, coordinator: Any) -> None:
        self.coord = coordinator
        self.status = "idle"
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._cid: str | None = None
        self._answer_sent = False
        self._signals: dict[str, asyncio.Future] = {}

    @property
    def busy(self) -> bool:
        return self._task is not None

    def _status(self, value: str) -> None:
        self.status = value
        self.coord.notify_reply_state()

    def _require_ringing(self) -> None:
        if not self._cid or self.coord.active_call_id != self._cid:
            raise ReplyError("The incoming call is no longer active.")
        if self.coord.doorbell_state != "ringing":
            raise ReplyError("Reply is available only while a visitor is ringing.")

    def on_signal(self, mid: str, raw: dict) -> None:
        if not self.busy:
            return
        head = raw.get("HEADER", {}) or {}
        cid = head.get("CID")
        if mid == "23001" and cid and cid != self._cid:
            self.cancel()
            return
        if cid != self._cid:
            return
        if mid == "24002":
            self.cancel()
        elif mid == "24000" and str(head.get("RSLT")) == "200":
            sender = head.get("CLIID")
            if sender and sender != self.coord.cliid:
                self.cancel()
            elif self._answer_sent:
                future = self._signals.get("accepted")
                if future and not future.done():
                    future.set_result(raw)
        elif mid == "31001":
            future = self._signals.get(mid)
            if future and not future.done():
                future.set_result(raw)

    def cancel(self) -> None:
        if self._task and not self._task.done() and not self._task.cancelling():
            self._task.cancel()

    async def stop(self) -> None:
        task = self._task
        if task:
            self.cancel()
            try:
                await task
            except (asyncio.CancelledError, ReplyError):
                pass

    async def play(self, directory: Path, filename: str) -> None:
        if self.busy:
            raise ReplyError("A prerecorded reply is already in progress.")
        self._cid = self.coord.active_call_id
        self._require_ringing()
        self.last_error = None
        self._task = asyncio.create_task(self._run(directory, filename))
        self.coord.notify_reply_state()
        try:
            await self._task
        except asyncio.CancelledError:
            self._status("stopped")
            if asyncio.current_task().cancelling():
                raise
            raise ReplyError(
                "Reply stopped because the call ended or was taken elsewhere."
            ) from None
        except Exception as err:
            self.last_error = (
                str(err)
                if isinstance(err, ReplyError)
                else "Could not prepare or send the reply audio."
            )
            self._status("failed")
            if isinstance(err, ReplyError):
                raise
            raise ReplyError(self.last_error) from err
        finally:
            self._task = None
            self._cid = None
            self._answer_sent = False
            for future in self._signals.values():
                if not future.done():
                    future.cancel()
            self._signals.clear()
            self.coord.notify_reply_state()

    def _publish(self, mid: str, body: dict | None = None, **fields: Any) -> None:
        if self.coord.active_call_id != self._cid:
            raise ReplyError("The incoming call is no longer active.")
        header = {
            "MID": mid,
            "VER": "1.0",
            "SYS": "2",
            "CLIID": self.coord.cliid,
            "RCVTPC": self.coord.rcvtpc,
            "RSLT": 0,
            "SID": datetime.now().strftime("%Y%m%d%H%M%S%f"),
            "CID": self._cid,
            "LEN": len((body or {}).get("SDP", "")),
            **fields,
        }
        if mid == "30001":
            header["RLS"] = "0"
        if not self.coord.publish({"HEADER": header, "BODY": body or {}}):
            raise ReplyError("Aiphone cloud messaging is not connected.")

    async def _run(self, directory: Path, filename: str) -> None:
        self._status("preparing")
        # Shield opening so cancellation cannot abandon an opened media source.
        opening = asyncio.create_task(
            asyncio.to_thread(_open_audio, directory, filename)
        )
        try:
            player = await asyncio.shield(opening)
        except asyncio.CancelledError:
            try:
                player = await opening
                await asyncio.to_thread(_close_player, player)
            except Exception:
                pass
            raise
        except ValueError as err:
            raise ReplyError(str(err)) from err
        if player.audio is None:
            await asyncio.to_thread(_close_player, player)
            raise ReplyError("The selected file has no audio track.")
        pc = None
        track = GatedAudioTrack(player.audio)
        try:
            self._require_ringing()
            pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            sender = pc.addTrack(track)
            connected = asyncio.Event()
            self._signals = {
                key: asyncio.get_running_loop().create_future()
                for key in ("31001", "accepted")
            }

            @pc.on("connectionstatechange")
            def state_changed() -> None:
                if pc.connectionState in ("connected", "failed", "closed"):
                    connected.set()

            self._status("connecting")
            await pc.setLocalDescription(await pc.createOffer())
            sdp = "\r\n".join(
                line
                for line in pc.localDescription.sdp.split("\r\n")
                if not line.startswith("a=fingerprint:")
                or line.startswith("a=fingerprint:sha-256")
            )
            self._require_ringing()
            self._publish("30001", {"SDP": sdp})
            answer = await asyncio.wait_for(self._signals["31001"], SIGNAL_TIMEOUT)
            answer_sdp = (answer.get("BODY") or {}).get("SDP")
            if not answer_sdp:
                raise ReplyError(
                    "The cloud did not return an audio publisher SDP answer."
                )
            await pc.setRemoteDescription(RTCSessionDescription(answer_sdp, "answer"))
            if not any(
                t.sender is sender and t.currentDirection in ("sendonly", "sendrecv")
                for t in pc.getTransceivers()
            ):
                raise ReplyError("The cloud did not negotiate an audio-send direction.")
            await asyncio.wait_for(connected.wait(), CONNECTION_TIMEOUT)
            if pc.connectionState != "connected":
                raise ReplyError("The audio publisher connection failed.")
            self._require_ringing()
            self._answer_sent = True
            self._publish(
                "24000",
                CKIND=1,
                RECKIND=0,
                AVID=0,
                RSLT=200,
                REASON=0,
                SNDTPC=self.coord.sndtpc,
            )
            self._status("accepting")
            # Matching MQTT acceptance is a protocol observation, not proof of
            # audible playback. Firmware that does not echo it will time out.
            await asyncio.wait_for(self._signals["accepted"], SIGNAL_TIMEOUT)
            if self.coord.active_call_id != self._cid:
                raise ReplyError("The incoming call is no longer active.")
            track.begin()
            self._status("sending")
            await asyncio.wait_for(track.finished.wait(), MAX_REPLY_SECONDS + 2)
            if track.error or not track.file_samples:
                raise ReplyError(track.error or "No audio frames were sent.")
            await asyncio.sleep(0.3)  # allow the final audio packet to leave
            report = await pc.getStats()
            packets = sum(
                getattr(stat, "packetsSent", 0)
                for stat in report.values()
                if stat.type == "outbound-rtp"
            )
            if not packets:
                raise ReplyError("No outbound RTP packets were reported.")
            _LOGGER.info(
                "Reply sender completed: %.2fs audio; %s RTP packets (speaker unverified)",
                track.file_samples / 48000,
                packets,
            )
            self._status("sent")
        except asyncio.TimeoutError as err:
            raise ReplyError(
                "Timed out waiting for call audio signaling or playback."
            ) from err
        finally:
            track.stop()
            try:
                if pc:
                    await pc.close()
            finally:
                await asyncio.to_thread(_close_player, player)


def _close_player(player: PreparedAudio) -> None:
    player.audio.stop()
