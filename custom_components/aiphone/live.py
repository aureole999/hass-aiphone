"""Share incoming audio/video with the recorder and native HA WebRTC viewers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from aiortc.sdp import candidate_from_sdp


class _ViewerTrack(MediaStreamTrack):
    def __init__(self, proxy: MediaStreamTrack, owner: LiveVideoRelay) -> None:
        super().__init__()
        self.kind = proxy.kind
        self._proxy = proxy
        self._owner = owner

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        return await self._proxy.recv()

    def stop(self) -> None:
        self._proxy.stop()
        self._owner.viewers.discard(self)
        self._owner.viewers_changed.set()
        super().stop()


class LiveVideoRelay:
    """Bound viewer buffering; never let browsers compete with recording for recv()."""

    def __init__(self) -> None:
        self._relay = MediaRelay()
        self._source: MediaStreamTrack | None = None
        self._audio_source: MediaStreamTrack | None = None
        self.ready = asyncio.Event()
        self.viewers: set[_ViewerTrack] = set()
        self.viewers_changed = asyncio.Event()

    def attach(self, source: MediaStreamTrack) -> MediaStreamTrack:
        # SDP may announce audio before video. Replacing one kind must not
        # discard the other kind or disconnect its existing viewers.
        for track in tuple(self.viewers):
            if track.kind == source.kind:
                track.stop()
        if source.kind == "audio":
            self._audio_source = source
        else:
            self._source = source
            self.ready.set()
        return self._relay.subscribe(source)

    async def subscribe(self, timeout: float = 30) -> MediaStreamTrack:
        await asyncio.wait_for(self.ready.wait(), timeout)
        source = self._source
        if source is None or source.readyState != "live":
            raise MediaStreamError("The doorbell video session has ended")
        track = _ViewerTrack(self._relay.subscribe(source, buffered=False), self)
        self.viewers.add(track)
        self.viewers_changed.set()
        return track

    def subscribe_audio(self) -> MediaStreamTrack | None:
        """Use an announced audio track without waiting for its first RTP packet."""
        source = self._audio_source
        if source is None or source.readyState != "live":
            return None
        track = _ViewerTrack(self._relay.subscribe(source, buffered=False), self)
        self.viewers.add(track)
        self.viewers_changed.set()
        return track

    async def hold_monitor(self, duration: float, reconnect_grace: float = 0) -> None:
        """Keep an explicitly opened monitor alive while anyone is watching."""
        await asyncio.sleep(duration)
        while True:
            while self.viewers:
                self.viewers_changed.clear()
                await self.viewers_changed.wait()
            if not reconnect_grace:
                return
            self.viewers_changed.clear()
            try:
                await asyncio.wait_for(self.viewers_changed.wait(), reconnect_grace)
            except TimeoutError:
                return

    def end(self) -> None:
        self.ready.clear()
        self._source = None
        self._audio_source = None
        for track in tuple(self.viewers):
            track.stop()


@dataclass
class _Session:
    pc: RTCPeerConnection
    setup: asyncio.Task | None = None
    tracks: list[MediaStreamTrack] = field(default_factory=list)
    remote_ready: bool = False
    candidates: list[Any] = field(default_factory=list)
    microphone: MediaStreamTrack | None = None
    talking: bool = False


class BrowserVideo:
    """Share downlink media and optionally bind an explicitly offered microphone."""

    def __init__(
        self,
        get_track: Callable[[], Awaitable[MediaStreamTrack]],
        get_audio_track: Callable[[], MediaStreamTrack | None] | None = None,
        start_talk: Callable[[MediaStreamTrack, str], Awaitable[None]] | None = None,
        stop_talk: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._get_track = get_track
        self._get_audio_track = get_audio_track
        self._start_talk = start_talk
        self._stop_talk = stop_talk
        self._sessions: dict[str, _Session] = {}

    async def offer(self, sdp: str, session_id: str, answer: Callable[[str], None]) -> None:
        await self.close(session_id)
        session = _Session(RTCPeerConnection(RTCConfiguration(iceServers=[])))
        self._sessions[session_id] = session

        @session.pc.on("connectionstatechange")
        async def connection_changed():
            if session.pc.connectionState in ("failed", "closed"):
                await self.close(session_id)

        @session.pc.on("track")
        def microphone(track):
            if track.kind == "audio":
                session.microphone = track

                @track.on("ended")
                def mic_ended():
                    asyncio.create_task(self.close(session_id))

        session.setup = asyncio.create_task(self._answer(session_id, session, sdp, answer))
        try:
            await session.setup
        except asyncio.CancelledError:
            if self._sessions.get(session_id) is session:
                await self.close(session_id)
                raise
        except Exception:
            await self.close(session_id)
            raise

    async def _answer(self, session_id, session, sdp, answer) -> None:
        pc = session.pc
        await pc.setRemoteDescription(RTCSessionDescription(sdp, "offer"))
        session.remote_ready = True
        for candidate in session.candidates:
            await self._add_candidate(pc, candidate)
        session.candidates.clear()
        transceivers = pc.getTransceivers()
        if not any(t.kind == "video" and t.direction != "inactive" for t in transceivers):
            raise ValueError("The browser did not offer a video track")
        track = await self._get_track()
        session.tracks.append(track)

        @track.on("ended")
        def track_ended():
            asyncio.create_task(self.close(session_id))

        pc.addTrack(track)
        if session.microphone and self._start_talk:
            await self._start_talk(session.microphone, session_id)
            session.talking = True
        for transceiver in transceivers:
            if transceiver.kind == "video":
                transceiver.direction = "sendonly"
            elif transceiver.kind == "audio":
                audio = self._get_audio_track() if self._get_audio_track else None
                microphone = bool(session.microphone and self._start_talk)
                if audio is None:
                    transceiver.direction = "recvonly" if microphone else "inactive"
                else:
                    session.tracks.append(audio)
                    pc.addTrack(audio)
                    transceiver.direction = "sendrecv" if microphone else "sendonly"
        await pc.setLocalDescription(await pc.createAnswer())
        if self._sessions.get(session_id) is session:
            answer(pc.localDescription.sdp)

    async def candidate(self, session_id: str, candidate: Any) -> None:
        if (session := self._sessions.get(session_id)) is None:
            return
        if not session.remote_ready:
            session.candidates.append(candidate)
            return
        await self._add_candidate(session.pc, candidate)

    @staticmethod
    async def _add_candidate(pc: RTCPeerConnection, candidate: Any) -> None:
        value = candidate.candidate
        if not value:
            await pc.addIceCandidate(None)
            return
        parsed = candidate_from_sdp(value.removeprefix("candidate:"))
        parsed.sdpMid = candidate.sdp_mid
        parsed.sdpMLineIndex = candidate.sdp_m_line_index
        await pc.addIceCandidate(parsed)

    async def close(self, session_id: str) -> None:
        if (session := self._sessions.pop(session_id, None)) is None:
            return
        if session.setup and not session.setup.done() and session.setup is not asyncio.current_task():
            session.setup.cancel()
            await asyncio.gather(session.setup, return_exceptions=True)
        for track in session.tracks:
            track.stop()
        if session.microphone:
            session.microphone.stop()
        await session.pc.close()
        if session.talking and self._stop_talk:
            await self._stop_talk(session_id)

    async def aclose(self) -> None:
        for session_id in tuple(self._sessions):
            await self.close(session_id)
