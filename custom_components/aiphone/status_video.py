"""Continuous status video and audio over HA's native WebRTC connection."""
from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import datetime
from fractions import Fraction
from zoneinfo import ZoneInfo

import av
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from PIL import Image, ImageDraw, ImageFont, ImageOps

_LOGGER = logging.getLogger(__name__)


def _render(video, cached: bytes):
    source = video._latest_video_frame
    live = video.session_cid is not None and source is not None
    image = source.to_image() if live else Image.open(io.BytesIO(cached))
    image = ImageOps.pad(image.convert("RGB"), (640, 480))
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=20)
    draw.rectangle((0, 0, 640, 68), fill="#121923")
    draw.rectangle((0, 446, 640, 480), fill="#121923")
    draw.text((12, 5), "AIPHONE  EXPERIMENTAL", font=font, fill="#a0b5cd")
    status = video.call_status
    color = "#ff8a8a" if status in ("offline", "audio_interrupted") else "#ffd371" if status in (
        "ringing", "requesting_call", "connecting_audio", "preparing_reply"
    ) else "#8fe7bd"
    connection = "Connected" if video.coord.mqtt_connected else "Offline"
    draw.text((12, 34), f"{connection} | {status.replace('_', ' ').capitalize()}", font=font, fill=color)
    captured = time.time() if live else video.snapshot.captured_at
    zone = ZoneInfo(getattr(video.coord.hass.config, "time_zone", "UTC"))
    stamp = datetime.fromtimestamp(captured, zone).strftime("%Y-%m-%d %H:%M:%S %Z") if captured else "No image available"
    footer = f"Live | {stamp}" if live else f"Snapshot | {stamp}"
    if live and time.monotonic() - video._video_received_at > 1.5:
        footer = "Live | Waiting for video"
    draw.text((12, 451), footer, font=font, fill="#b4c2d3")
    return av.VideoFrame.from_image(image)


class StatusVideoTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, video) -> None:
        super().__init__()
        self.video = video
        self.frames = 0
        self.started = time.monotonic()
        self.video.live.external_viewers += 1
        self.video.live.viewers_changed.set()
        self._opening = asyncio.create_task(self._start())

    async def _start(self):
        try:
            lease = await self.video.async_viewer_track()
            lease.stop()
        except (RuntimeError, TimeoutError, MediaStreamError):
            _LOGGER.debug("Status video could not open the doorbell connection", exc_info=True)

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        await asyncio.sleep(max(0, self.started + self.frames / 10 - time.monotonic()))
        frame = await asyncio.to_thread(_render, self.video, await self.video.snapshot.picture())
        frame.pts, frame.time_base = self.frames * 9000, Fraction(1, 90000)
        self.frames += 1
        return frame

    def stop(self):
        if self.readyState != "live":
            return
        self.video.live.external_viewers -= 1
        self.video.live.viewers_changed.set()
        # The shared media setup may serve another viewer. Release its temporary
        # subscription when it finishes rather than cancelling the shared call.
        super().stop()


class StatusAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, video):
        super().__init__()
        self.video, self.samples, self.sequence = video, 0, -1
        self.started = time.monotonic()
        self.resampler = av.AudioResampler(format="s16", layout="mono", rate=8000, frame_size=160)

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        await asyncio.sleep(max(0, self.started + self.samples / 8000 - time.monotonic()))
        frame = None
        source = self.video._latest_audio_frame
        if source is not None and self.sequence != self.video._audio_frames and time.monotonic() - self.video._audio_received_at < .15:
            self.sequence = self.video._audio_frames
            copy = av.AudioFrame(format=source.format.name, layout=source.layout.name, samples=source.samples)
            for target, original in zip(copy.planes, source.planes):
                target.update(bytes(original))
            copy.sample_rate = source.sample_rate
            converted = self.resampler.resample(copy)
            if converted:
                frame = converted[-1]
        if frame is None:
            frame = av.AudioFrame(format="s16", layout="mono", samples=160)
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
        frame.sample_rate = 8000
        frame.pts, frame.time_base = self.samples, Fraction(1, 8000)
        self.samples += frame.samples
        return frame
