"""Passive JPEG cache shared by camera, image and OSD consumers."""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import time
from pathlib import Path

_LOGGER = logging.getLogger(__name__)
PLACEHOLDER = Path(__file__).parent / "assets" / "no_snapshot.jpg"


def frame_jpeg(frame) -> bytes:
    output = io.BytesIO()
    frame.to_image().convert("RGB").save(output, format="JPEG", quality=85)
    return output.getvalue()


class SnapshotCache:
    def __init__(self, directory: Path, identity: str, on_change=None) -> None:
        self.path = directory / (hashlib.sha256(identity.encode()).hexdigest()[:16] + ".jpg")
        self.on_change = on_change
        self.jpeg: bytes | None = None
        self.captured_at: float | None = None
        self.placeholder: bytes | None = None
        self._task: asyncio.Task | None = None
        self._last_capture = self._last_save = 0.0

    async def load(self) -> None:
        def read():
            self.placeholder = PLACEHOLDER.read_bytes()
            try:
                from PIL import Image
                image = self.path.read_bytes()
                with Image.open(io.BytesIO(image)) as check:
                    check.verify()
                self.jpeg, self.captured_at = image, self.path.stat().st_mtime
            except (OSError, ValueError):
                pass
        await asyncio.to_thread(read)

    def observe(self, frame) -> None:
        now = time.monotonic()
        if now - self._last_capture < 1 or self._task and not self._task.done():
            return
        self._last_capture = now
        self._task = asyncio.create_task(self._capture(frame))

    async def _capture(self, frame) -> None:
        try:
            self.jpeg = await asyncio.to_thread(frame_jpeg, frame)
            self.captured_at = time.time()
            if self.on_change:
                self.on_change()
            if time.monotonic() - self._last_save >= 30:
                await self.save()
        except Exception:
            _LOGGER.exception("Could not update snapshot cache")

    async def save(self) -> None:
        image, captured = self.jpeg, self.captured_at
        if not image:
            return
        def write():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_bytes(image)
            os.replace(temporary, self.path)
            if captured:
                os.utime(self.path, (captured, captured))
        try:
            await asyncio.to_thread(write)
            self._last_save = time.monotonic()
        except OSError:
            _LOGGER.warning("Could not save snapshot cache", exc_info=True)

    async def flush(self, frame=None) -> None:
        if self._task:
            await self._task
        if frame is not None:
            await self._capture(frame)
        await self.save()

    async def picture(self) -> bytes:
        if self.placeholder is None:
            await self.load()
        return self.jpeg or self.placeholder
