from __future__ import annotations

import asyncio
import threading
from contextlib import suppress
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from src.live.audio_sources import is_rtc_stream_url


def rtc_dependency_error() -> str:
    try:
        import aiortc  # noqa: F401
        import av  # noqa: F401
    except Exception as exc:
        return f"RTC audio support requires aiortc/av: {exc}"
    return ""


def build_rtc_proxy_url(source_url: str) -> str:
    parsed = urlparse(str(source_url or "").strip())
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError(f"invalid rtc source url: {source_url}")
    return f"https://{host}:10443/player"


class PCMFrameConverter:
    def __init__(self, *, sample_rate: int = 16000, layout: str = "mono") -> None:
        dependency_error = rtc_dependency_error()
        if dependency_error:
            raise RuntimeError(dependency_error)
        from av import AudioResampler

        self._resampler = AudioResampler(format="s16", layout=layout, rate=max(8000, int(sample_rate)))

    def convert(self, frame: Any) -> list[bytes]:
        resampled = self._resampler.resample(frame)
        if not resampled:
            return []
        if not isinstance(resampled, list):
            resampled = [resampled]
        return [b"".join(bytes(plane) for plane in item.planes) for item in resampled]


class WebRTCAudioPullSession:
    def __init__(
        self,
        *,
        source_url: str,
        proxy_url: str = "",
        request_timeout_sec: float = 10.0,
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        source = str(source_url or "").strip()
        if not is_rtc_stream_url(source):
            raise ValueError(f"unsupported RTC source url: {source_url}")

        dependency_error = rtc_dependency_error()
        if dependency_error:
            raise RuntimeError(dependency_error)

        self.source_url = source
        self.proxy_url = str(proxy_url or "").strip() or build_rtc_proxy_url(source)
        self.request_timeout_sec = max(3.0, float(request_timeout_sec))
        self._log_fn = log_fn or (lambda _msg: None)

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pc = None
        self._shutdown_event = None
        self._on_audio_frame: Callable[[Any], None] | None = None

        self._ready_event = threading.Event()
        self._done_event = threading.Event()
        self._stop_event = threading.Event()
        self._stop_lock = threading.Lock()

        self._state_lock = threading.Lock()
        self._last_error = ""

    @property
    def last_error(self) -> str:
        with self._state_lock:
            return self._last_error

    def start(self, *, on_audio_frame: Callable[[Any], None]) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            return
        self._on_audio_frame = on_audio_frame
        self._ready_event.clear()
        self._done_event.clear()
        self._stop_event.clear()
        with self._state_lock:
            self._last_error = ""
        thread = threading.Thread(target=self._run_sync, name="rtc-audio-pull", daemon=True)
        self._thread = thread
        thread.start()

    def wait_until_ready(self, *, timeout_sec: float = 10.0) -> tuple[bool, str]:
        ok = self._ready_event.wait(max(0.1, float(timeout_sec)))
        if ok:
            return True, ""
        return False, self.last_error or "rtc audio track not ready before timeout"

    def wait(self, *, timeout_sec: float | None = None) -> bool:
        return self._done_event.wait(timeout_sec)

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive() and not self._done_event.is_set())

    def stop(self, *, timeout_sec: float = 3.0) -> None:
        timeout = max(0.5, float(timeout_sec))
        with self._stop_lock:
            self._stop_event.set()
            loop = self._loop
            shutdown_event = self._shutdown_event
            if loop is not None and not loop.is_closed() and shutdown_event is not None:
                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(shutdown_event.set)

            thread = self._thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=timeout)

            thread = self._thread
            if thread is not None and thread.is_alive():
                # Fallback for edge cases where graceful shutdown is delayed.
                loop = self._loop
                pc = self._pc
                if loop is not None and not loop.is_closed() and pc is not None:
                    with suppress(Exception):
                        future = asyncio.run_coroutine_threadsafe(pc.close(), loop)
                        future.result(timeout=timeout)
                if thread.is_alive():
                    thread.join(timeout=timeout)

    def _set_error(self, message: str) -> None:
        text = str(message or "").strip()
        if not text:
            return
        with self._state_lock:
            if not self._last_error:
                self._last_error = text

    def _run_sync(self) -> None:
        try:
            asyncio.run(self._run_async())
        except Exception as exc:
            if not self._stop_event.is_set():
                self._set_error(f"rtc audio startup failed: {exc}")
                self._log_fn(f"[rtc-audio] {self.last_error}")
        finally:
            self._loop = None
            self._pc = None
            self._shutdown_event = None
            self._done_event.set()

    async def _run_async(self) -> None:
        from aiortc import RTCPeerConnection, RTCSessionDescription

        pc = RTCPeerConnection()
        self._pc = pc
        self._loop = asyncio.get_running_loop()
        shutdown_event = asyncio.Event()
        self._shutdown_event = shutdown_event
        audio_task = None

        @pc.on("track")
        def on_track(track) -> None:
            nonlocal audio_task
            if getattr(track, "kind", "") != "audio" or audio_task is not None:
                return
            audio_task = asyncio.create_task(self._consume_audio_track(track, shutdown_event))

        @pc.on("connectionstatechange")
        def on_connectionstatechange() -> None:
            state = str(getattr(pc, "connectionState", "") or "").strip().lower()
            if state in {"failed", "closed"} and not self._stop_event.is_set():
                self._set_error(f"rtc connection state={state}")
                shutdown_event.set()

        pc.addTransceiver("audio", direction="recvonly")
        pc.addTransceiver("video", direction="recvonly")

        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        response = requests.post(
            self.proxy_url,
            json={
                "url": self.source_url,
                "clientip": None,
                "sdp": pc.localDescription.sdp if pc.localDescription else offer.sdp,
            },
            timeout=self.request_timeout_sec,
        )
        response.raise_for_status()
        payload = response.json()
        payload_obj = payload if isinstance(payload, dict) else {}
        answer_sdp = str(payload_obj.get("sdp") or "").strip()
        if not answer_sdp:
            raise RuntimeError("rtc proxy returned empty sdp")
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))

        try:
            while not self._stop_event.is_set() and not shutdown_event.is_set():
                await asyncio.sleep(0.2)
        finally:
            if audio_task is not None:
                audio_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await audio_task
            with suppress(Exception):
                await pc.close()

    async def _consume_audio_track(self, track, shutdown_event) -> None:
        try:
            while not self._stop_event.is_set():
                frame = await track.recv()
                self._ready_event.set()
                callback = self._on_audio_frame
                if callback is not None:
                    callback(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._stop_event.is_set():
                self._set_error(f"rtc audio receive failed: {exc}")
                shutdown_event.set()
