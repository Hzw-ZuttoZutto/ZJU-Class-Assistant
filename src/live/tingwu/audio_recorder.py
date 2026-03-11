from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from shutil import which
from typing import Callable


def _format_ts(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y%m%d_%H%M%S")


@dataclass
class AudioRecordingConfig:
    poll_interval_sec: float = 1.0
    max_lag_sec: float = 10.0


@dataclass
class AudioSessionMeta:
    course_title: str
    teacher_name: str
    session_dir: Path
    started_at: datetime


@dataclass
class AudioSegment:
    index: int
    started_at: datetime
    ended_at: datetime
    path: Path
    reason: str = ""

    def to_json(self) -> dict[str, object]:
        return {
            "index": self.index,
            "started_at_local": _format_ts(self.started_at),
            "ended_at_local": _format_ts(self.ended_at),
            "path": str(self.path),
            "reason": self.reason,
        }


@dataclass
class AudioRecordingResult:
    final_mp3_path: Path | None
    segment_count: int
    success: bool
    error: str
    report_path: Path


class AudioRecorderBackend:
    def __init__(self) -> None:
        self.ffmpeg = which("ffmpeg") or ""
        self.ffprobe = which("ffprobe") or ""

    def ensure_available(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)

    def probe_audio(self, stream_url: str, *, timeout_sec: float = 4.0) -> bool:
        if not stream_url:
            return False
        cmd = [
            self.ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-of",
            "json",
            stream_url,
        ]
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=max(1.0, float(timeout_sec)),
            )
        except subprocess.SubprocessError:
            return False
        if proc.returncode != 0:
            return False
        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return False
        streams = payload.get("streams")
        if not isinstance(streams, list):
            return False
        for item in streams:
            if isinstance(item, dict) and str(item.get("codec_type") or "") == "audio":
                return True
        return False

    def start_capture(self, stream_url: str, output_path: Path) -> subprocess.Popen:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-y",
            "-rw_timeout",
            "10000000",
            "-fflags",
            "+discardcorrupt",
            "-i",
            stream_url,
            "-map",
            "0:a:0",
            "-vn",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "4",
            str(output_path),
        ]
        return subprocess.Popen(  # noqa: S603
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop_capture(self, proc: subprocess.Popen, *, grace_sec: float = 3.0) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=max(0.5, float(grace_sec)))
        except Exception:
            proc.kill()
            proc.wait(timeout=1.0)

    def merge_mp3_segments(self, segments: list[Path], output_mp3: Path) -> bool:
        if not segments:
            return False
        output_mp3.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as fp:
            concat_file = Path(fp.name)
            for segment in segments:
                escaped = str(segment).replace("'", "'\\''")
                fp.write(f"file '{escaped}'\n")
        try:
            cmd = [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-vn",
                "-c:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(output_mp3),
            ]
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
            return proc.returncode == 0 and output_mp3.exists() and output_mp3.stat().st_size > 0
        finally:
            try:
                concat_file.unlink()
            except OSError:
                pass


class AudioOnlyRecorderService:
    def __init__(
        self,
        *,
        poller,
        config: AudioRecordingConfig,
        session_meta: AudioSessionMeta,
        backend: AudioRecorderBackend | None = None,
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.poller = poller
        self.config = config
        self.session_meta = session_meta
        self.backend = backend or AudioRecorderBackend()
        self._log_fn = log_fn or print
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._tmp_dir = self.session_meta.session_dir / "_tingwu_audio_tmp"
        self._segments: list[AudioSegment] = []
        self._segment_index = 0
        self._active_proc: subprocess.Popen | None = None
        self._active_path: Path | None = None
        self._active_started_at: datetime | None = None
        self._active_url = ""
        self._active_last_size = 0
        self._active_last_growth_mono = 0.0

        self._result: AudioRecordingResult | None = None

    def startup_check(self, *, timeout_sec: float = 20.0) -> tuple[bool, str]:
        if not self.backend.ensure_available():
            return False, "ffmpeg/ffprobe is required but not found in PATH"
        deadline = time.monotonic() + max(1.0, float(timeout_sec))
        while time.monotonic() < deadline:
            url = self._teacher_stream_url()
            if not url:
                time.sleep(0.8)
                continue
            if self.backend.probe_audio(url, timeout_sec=3.0):
                return True, ""
            time.sleep(0.8)
        return False, "teacher stream with audio not detected before startup timeout"

    def start(self) -> None:
        self.session_meta.session_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="tingwu-audio-recorder", daemon=True)
        self._thread.start()

    def stop(self) -> AudioRecordingResult:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join()
        self._thread = None
        if self._result is None:
            self._result = self._build_result(error="recording thread stopped unexpectedly")
        return self._result

    def _run(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.now().astimezone()
            url = self._teacher_stream_url()
            if url and self.backend.probe_audio(url, timeout_sec=2.0):
                self._ensure_capture(url=url, now=now)
            else:
                self._stop_capture(now=now, reason="stream_missing_audio")
            self._stop_event.wait(max(0.2, float(self.config.poll_interval_sec)))
        self._stop_capture(now=datetime.now().astimezone(), reason="session_stopped")
        self._result = self._build_result(error="")

    def _ensure_capture(self, *, url: str, now: datetime) -> None:
        if self._active_proc is None:
            self._start_capture(url=url, now=now)
            return
        if url != self._active_url:
            self._stop_capture(now=now, reason="stream_url_changed")
            self._start_capture(url=url, now=now)
            return
        if self._is_capture_stalled():
            self._stop_capture(now=now, reason="capture_stalled")
            self._start_capture(url=url, now=now)

    def _start_capture(self, *, url: str, now: datetime) -> None:
        self._segment_index += 1
        seg_name = f"segment_{self._segment_index:05d}_{_format_ts(now)}.writing.mp3"
        seg_path = self._tmp_dir / seg_name
        try:
            proc = self.backend.start_capture(url, seg_path)
        except Exception as exc:
            self._log(f"[tingwu][audio] start capture failed: {exc}")
            return
        self._active_proc = proc
        self._active_path = seg_path
        self._active_started_at = now
        self._active_url = url
        self._active_last_size = 0
        self._active_last_growth_mono = time.monotonic()
        self._log(f"[tingwu][audio] capture started #{self._segment_index} at {_format_ts(now)}")

    def _stop_capture(self, *, now: datetime, reason: str) -> None:
        proc = self._active_proc
        seg_path = self._active_path
        started_at = self._active_started_at

        self._active_proc = None
        self._active_path = None
        self._active_started_at = None
        self._active_url = ""
        self._active_last_size = 0
        self._active_last_growth_mono = 0.0

        if proc is None:
            return
        try:
            self.backend.stop_capture(proc)
        except Exception:
            pass
        if seg_path is None or started_at is None:
            return
        final_path = seg_path.with_name(seg_path.name.replace(".writing", ""))
        try:
            if seg_path.exists() and seg_path.stat().st_size > 0:
                os.replace(seg_path, final_path)
            elif final_path.exists() and final_path.stat().st_size > 0:
                pass
            else:
                seg_path.unlink(missing_ok=True)
                return
        except OSError:
            return
        self._segments.append(
            AudioSegment(
                index=len(self._segments) + 1,
                started_at=started_at,
                ended_at=now,
                path=final_path,
                reason=reason,
            )
        )

    def _is_capture_stalled(self) -> bool:
        if self._active_proc is None or self._active_path is None:
            return False
        if self._active_proc.poll() is not None:
            return True
        now_mono = time.monotonic()
        if self._active_path.exists():
            size = self._active_path.stat().st_size
            if size > self._active_last_size:
                self._active_last_size = size
                self._active_last_growth_mono = now_mono
                return False
        return (now_mono - self._active_last_growth_mono) > max(1.0, float(self.config.max_lag_sec))

    def _teacher_stream_url(self) -> str:
        try:
            snapshot = self.poller.get_snapshot()
        except Exception:
            return ""
        stream = getattr(snapshot, "streams", {}).get("teacher")
        if not stream:
            return ""
        return str(getattr(stream, "stream_m3u8", "") or "").strip()

    def _build_result(self, *, error: str) -> AudioRecordingResult:
        final_mp3 = self.session_meta.session_dir / "tingwu_audio_full.mp3"
        writing = self.session_meta.session_dir / "tingwu_audio_full.writing.mp3"
        for path in (writing, final_mp3):
            if path.exists():
                path.unlink(missing_ok=True)

        success = False
        err = str(error or "").strip()
        if not err and self._segments:
            segment_files = [seg.path for seg in self._segments if seg.path.exists() and seg.path.stat().st_size > 0]
            if segment_files:
                try:
                    success = self.backend.merge_mp3_segments(segment_files, writing)
                except Exception as exc:
                    err = f"merge failed: {exc}"
                    success = False
                if success:
                    try:
                        os.replace(writing, final_mp3)
                    except OSError as exc:
                        err = f"rename merged mp3 failed: {exc}"
                        success = False
                        writing.unlink(missing_ok=True)
                else:
                    if not err:
                        err = "merge failed"
                    writing.unlink(missing_ok=True)
            else:
                err = "no_audio_segment_file"
        elif not self._segments and not err:
            err = "no_audio_segment"

        report = {
            "course_title": self.session_meta.course_title,
            "teacher_name": self.session_meta.teacher_name,
            "started_at_local": _format_ts(self.session_meta.started_at),
            "ended_at_local": _format_ts(datetime.now().astimezone()),
            "segment_count": len(self._segments),
            "success": bool(success),
            "error": err,
            "final_mp3_path": str(final_mp3),
            "segments": [seg.to_json() for seg in self._segments],
        }
        report_path = self.session_meta.session_dir / "tingwu_audio_recording_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return AudioRecordingResult(
            final_mp3_path=final_mp3 if success else None,
            segment_count=len(self._segments),
            success=bool(success),
            error=err,
            report_path=report_path,
        )

    def _log(self, msg: str) -> None:
        with self._lock:
            self._log_fn(msg)
