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

    A two-frame queue bounds microphone latency. Sources are exclusive; opening
    a second browser cannot silently take over the current microphone.
    """
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self.enabled = False
        self.owner: str | None = None
        self._source: MediaStreamTrack | None = None
        self._pump: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=2)
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
        self.source_ended.clear()
        self.error = None
        self._pump = asyncio.create_task(self._read(source))

    def release(self, owner: str) -> None:
        if self.owner != owner:
            return
        if self._pump:
            self._pump.cancel()
            self._pump = None
        if self._source:
            self._source.stop()
        self._source = self.owner = None
        while not self._queue.empty():
            self._queue.get_nowait()

    async def _read(self, source) -> None:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=8000, frame_size=160)
        try:
            while True:
                frame = await source.recv()
                for converted in resampler.resample(frame):
                    if not self.enabled:
                        continue
                    if self._queue.full():
                        self._queue.get_nowait()
                    self._queue.put_nowait(converted)
        except MediaStreamError:
            self.source_ended.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.error = "Could not decode microphone audio"
            self.source_ended.set()

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
        if self.enabled and not self._queue.empty():
            frame = self._queue.get_nowait()
            self.frames_sent += 1
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
        super().stop()
