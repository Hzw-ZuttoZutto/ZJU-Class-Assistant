from __future__ import annotations

import signal
import subprocess
import threading
from pathlib import Path
from shutil import which
from typing import Callable


class RealtimeAudioFrameReader:
    def __init__(
        self,
        *,
        frame_duration_ms: int = 100,
        ffmpeg_bin: str = "",
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.frame_duration_ms = max(20, int(frame_duration_ms))
        self.ffmpeg_bin = (ffmpeg_bin or "").strip() or (which("ffmpeg") or "")
        self._log_fn = log_fn or print
        self._lock = threading.Lock()
        self._active_source = ""
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def ensure_available(self) -> bool:
        return bool(self.ffmpeg_bin)

    @property
    def active_source(self) -> str:
        with self._lock:
            return self._active_source

    def start_stream_source(self, source_url: str, *, on_frame: Callable[[bytes], None]) -> None:
        source = str(source_url or "").strip()
        if not source:
            return
        with self._lock:
            if source == self._active_source and self._proc is not None and self._proc.poll() is None:
                return
        self.stop()
        cmd = self._build_stream_command(stream_url=source)
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._read_loop,
            args=(proc, on_frame),
            name="rt-audio-frame-reader",
            daemon=True,
        )
        with self._lock:
            self._proc = proc
            self._thread = thread
            self._active_source = source
        thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            proc = self._proc
            thread = self._thread
            self._proc = None
            self._thread = None
            self._active_source = ""
        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=1.5)
            except Exception:
                proc.kill()
                proc.wait(timeout=1.0)
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.5)

    def _build_stream_command(self, *, stream_url: str) -> list[str]:
        return [
            self.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-rw_timeout",
            "10000000",
            "-i",
            stream_url,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-",
        ]

    def _read_loop(self, proc: subprocess.Popen, on_frame: Callable[[bytes], None]) -> None:
        stdout = proc.stdout
        if stdout is None:
            return
        frame_bytes = max(320, int(16000 * 2 * self.frame_duration_ms / 1000))
        try:
            while not self._stop_event.is_set():
                chunk = stdout.read(frame_bytes)
                if not chunk:
                    break
                on_frame(chunk)
        except Exception as exc:
            self._log_fn(f"[rt-audio] frame reader failed: {exc}")


def build_mic_stream_ffmpeg_command(
    *,
    ffmpeg_bin: str,
    device: str,
    sample_rate: int = 16000,
) -> list[str]:
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-f",
        "dshow",
        "-i",
        f"audio={device}",
        "-ac",
        "1",
        "-ar",
        str(max(8000, int(sample_rate))),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
