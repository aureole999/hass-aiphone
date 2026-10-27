"""Prerecorded replies through the same app-compatible incoming-call session."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from fractions import Fraction
from pathlib import Path
from typing import Any

import av
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

from .reply_files import MAX_REPLY_SECONDS, resolve_reply_file

_LOGGER = logging.getLogger(__name__)


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
        self._remote_stop = False

    @property
    def busy(self) -> bool:
        return self._task is not None

    def _status(self, value: str) -> None:
        self.status = value
        self.coord.notify_reply_state()

    def on_signal(self, mid: str, raw: dict) -> None:
        if not self.busy:
            return
        head = raw.get("HEADER", {}) or {}
        cid = head.get("CID")
        if mid == "23001" and cid and cid != self._cid:
            self._remote_stop = True
            self.cancel()
            return
        if cid != self._cid:
            return
        if mid == "23003" or (mid == "23004" and not self.coord.video.is_own_cancel(head, cid)):
            self._remote_stop = True
            self.cancel()
        elif mid == "24002" and str(head.get("RSLT")) == "200":
            sender = head.get("CLIID")
            if sender and sender != self.coord.cliid:
                self._remote_stop = True
                self.cancel()

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
        if not self.coord.mqtt_connected:
            raise ReplyError("Aiphone is not connected")
        self._cid = self.coord.video.session_cid or self.coord.active_call_id
        self.last_error = None
        self._remote_stop = False
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
            self.coord.notify_reply_state()

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
        track = GatedAudioTrack(player.audio)
        video = self.coord.video
        try:
            self._status("connecting")
            lease = await video.async_viewer_track()
            lease.stop()
            self._cid = video.session_cid
            if not self._cid:
                raise ReplyError("The call has ended")
            self._status("accepting")
            if not await self.coord.async_answer_call():
                raise ReplyError("The call could not be answered")
            await video.wait_for_reply_audio(self._cid)
            if video._uplink is None:
                raise ReplyError("The audio publisher is unavailable")
            video._uplink.pause()
            video._uplink.max_queue_frames = 64
            video._uplink.claim("prerecorded-reply", track)
            track.begin()
            self._status("sending")
            before = video._uplink.frames_sent
            await asyncio.wait_for(track.finished.wait(), MAX_REPLY_SECONDS + 2)
            video._uplink.finish_source("prerecorded-reply")
            await video._uplink.drain()
            if track.error or not track.file_samples:
                raise ReplyError(track.error or "No audio frames were sent")
            if video._uplink.frames_sent <= before:
                raise ReplyError("No reply audio frames were transmitted")
            _LOGGER.info("Quick reply finished; call remains connected")
            self._status("sent")
        except (TimeoutError, RuntimeError) as err:
            raise ReplyError(str(err)) from err
        finally:
            if video._uplink:
                video._uplink.release("prerecorded-reply")
                video._uplink.max_queue_frames = video._uplink.MAX_QUEUE_FRAMES
                video._uplink.resume()
            track.stop()
            await asyncio.to_thread(_close_player, player)



def _close_player(player: PreparedAudio) -> None:
    player.audio.stop()
