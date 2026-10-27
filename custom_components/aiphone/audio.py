"""One bounded microphone/file source, paced as PCMU-compatible 20 ms PCM."""
from __future__ import annotations

import asyncio
from fractions import Fraction

import av
from aiortc import RTCRtpSender
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack


def prefer_pcmu(transceiver) -> None:
    codecs = [c for c in RTCRtpSender.getCapabilities("audio").codecs
              if c.mimeType.lower() == "audio/pcmu" and c.clockRate == 8000]
    if not codecs:
        raise RuntimeError("PCMU/8000 is unavailable")
    transceiver.setCodecPreferences(codecs)


class UplinkAudioTrack(MediaStreamTrack):
    """Drain input continuously, but send voice only after call acceptance.

    A small prebuffer absorbs browser/Wi-Fi jitter while a bounded queue caps
    end-to-end microphone latency. Sources are exclusive; opening a second
    browser cannot silently take over the current microphone.
    """
    kind = "audio"
    PREBUFFER_FRAMES = 2
    MAX_QUEUE_FRAMES = 8

    def __init__(self) -> None:
        super().__init__()
        self.enabled = False
        self.owner: str | None = None
        self._source: MediaStreamTrack | None = None
        self._pump: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.max_queue_frames = self.MAX_QUEUE_FRAMES
        self._paused = None
        self.last_frame_sent_at = 0.0
        self._primed = False
        self._pts = 0
        self._clock: float | None = None
        self.frames_sent = 0
        self.source_ended = asyncio.Event()
        self.error: str | None = None

    def claim(self, owner: str, source: MediaStreamTrack) -> None:
        if self.owner is not None and self.owner != owner:
            raise RuntimeError("Another microphone or prerecorded reply is already active")
        self.release(owner)
        self.owner, self._source = owner, source
        self._primed = False
        self.source_ended = asyncio.Event()
        self.error = None
        self._pump = asyncio.create_task(self._read(source, self.source_ended))

    def release(self, owner: str) -> None:
        if self._paused and self._paused[0] == owner:
            _, source, pump, _ = self._paused
            if pump:
                pump.cancel()
            source.stop()
            self._paused = None
        if self.owner != owner:
            return
        if self._pump:
            self._pump.cancel()
            self._pump = None
        if self._source:
            self._source.stop()
        self._source = self.owner = None
        self._primed = False
        while not self._queue.empty():
            self._queue.get_nowait()

    def pause(self) -> None:
        if self._paused or not self.owner:
            return
        self._paused = (self.owner, self._source, self._pump, self.source_ended)
        self.owner = self._source = self._pump = None
        self._primed = False
        while not self._queue.empty():
            self._queue.get_nowait()

    def resume(self) -> bool:
        paused, self._paused = self._paused, None
        if not paused:
            return False
        owner, source, pump, ended = paused
        if source.readyState != "live" or ended.is_set() or self.owner:
            if pump:
                pump.cancel()
            source.stop()
            return False
        self.owner, self._source, self._pump, self.source_ended = paused
        self._primed = False
        return True

    def finish_source(self, owner: str) -> None:
        """Stop reading a completed file while allowing queued audio to drain."""
        if self.owner == owner:
            if self._pump:
                self._pump.cancel()
                self._pump = None
            self.source_ended.set()

    async def drain(self, timeout: float = 3) -> None:
        async with asyncio.timeout(timeout):
            while not self._queue.empty() and self.readyState == "live":
                await asyncio.sleep(.02)
        await asyncio.sleep(.04)

    async def _read(self, source, ended: asyncio.Event) -> None:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=8000, frame_size=160)
        try:
            while True:
                frame = await source.recv()
                frame.pts = None
                for converted in resampler.resample(frame):
                    if self._source is not source:
                        continue
                    if not self.enabled:
                        self._primed = False
                        continue
                    if self._queue.qsize() >= self.max_queue_frames:
                        self._queue.get_nowait()
                    self._queue.put_nowait(converted)
        except MediaStreamError:
            ended.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._source is source:
                self.error = "Could not decode microphone audio"
            ended.set()

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        loop = asyncio.get_running_loop()
        if self._clock is None:
            self._clock = loop.time()
        target = self._clock + self._pts / 8000
        # Never burst old microphone frames after an event-loop stall.
        if loop.time() - target > .1:
            self._clock = loop.time() - self._pts / 8000
            target = loop.time()
        await asyncio.sleep(max(0, target - loop.time()))
        frame = None
        if self.enabled:
            if not self._primed and (
                self._queue.qsize() >= self.PREBUFFER_FRAMES or self.source_ended.is_set()
            ):
                self._primed = True
            if self._primed and not self._queue.empty():
                frame = self._queue.get_nowait()
                self.frames_sent += 1
                self.last_frame_sent_at = loop.time()
            elif self._queue.empty():
                self._primed = False
        if frame is None:
            frame = av.AudioFrame(format="s16", layout="mono", samples=160)
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
        frame.sample_rate = 8000
        frame.pts, frame.time_base = self._pts, Fraction(1, 8000)
        self._pts += frame.samples
        return frame

    def stop(self) -> None:
        self.enabled = False
        if self.owner:
            self.release(self.owner)
        if self._paused:
            self.release(self._paused[0])
        super().stop()
