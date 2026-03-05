from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from src.live.recording.ffmpeg_backend import FfmpegBackend
from src.live.recording.models import (
    GapEvent,
    RecordingConfig,
    SegmentManifest,
    SegmentPart,
    SessionMeta,
    build_session_folder_name,
    format_local_ts,
    sanitize_filename,
)


class LiveRecorderService:
    def __init__(
        self,
        *,
        poller,
        config: RecordingConfig,
        session_meta: SessionMeta,
        backend: FfmpegBackend | None = None,
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.poller = poller
        self.config = config
        self.session_meta = session_meta
        self.backend = backend or FfmpegBackend()
        self._log_fn = log_fn or print

        self._file_prefix = (
            f"{sanitize_filename(self.session_meta.course_title)}_"
            f"{sanitize_filename(self.session_meta.teacher_name)}"
        )

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._segment_index = 0
        self._segment: SegmentManifest | None = None
        self._segment_outputs: list[dict] = []

        self._active_proc = None
        self._active_path: Path | None = None
        self._active_start_at: datetime | None = None
        self._active_url: str = ""
        self._active_last_size = 0
        self._active_last_growth_mono = 0.0

        self._interruption_started_at: datetime | None = None
        self._interruption_reason: str = ""
        self._gap_open_started_at: datetime | None = None

    @staticmethod
    def build_session_dir(
        *,
        record_dir: str | None,
        course_title: str,
        teacher_name: str,
        started_at: datetime,
    ) -> Path:
        parent = Path(record_dir).expanduser().resolve() if record_dir else Path.cwd()
        folder = build_session_folder_name(course_title, teacher_name, started_at)
        return parent / folder

    def startup_check(self, timeout_sec: float) -> tuple[bool, str]:
        if not self.backend.ensure_available():
            return False, "ffmpeg/ffprobe is required but not found in PATH"

        deadline = time.monotonic() + max(1.0, timeout_sec)
        while time.monotonic() < deadline:
            url = self._teacher_stream_url()
            if not url:
                time.sleep(1.0)
                continue
            has_audio, has_video = self.backend.probe_av(url, timeout_sec=4.0)
            if has_audio and has_video:
                return True, ""
            time.sleep(1.0)
        return False, "teacher stream with both audio/video not detected before startup timeout"

    def start(self) -> None:
        self.session_meta.session_dir.mkdir(parents=True, exist_ok=True)
        (self.session_meta.session_dir / "_tmp").mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()

        self._thread = threading.Thread(target=self._run, name="live-recorder")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            # Finalization may involve ffmpeg concat/transcode and can exceed a short timeout.
            # We wait until the recorder thread completes to avoid leaving *.writing artifacts.
            thread.join()
        self._thread = None

    def _run(self) -> None:
        now = self._now()
        self._open_new_segment(now)
        while not self._stop_event.is_set():
            now = self._now()
            self._maybe_roll_segment(now)
            self._tick(now)
            self._stop_event.wait(max(0.25, self.config.poll_interval_sec))

        now = self._now()
        self._stop_active_capture(now)
        self._flush_open_gap_if_needed(now, finalizing=True)
        self._finalize_current_segment(now)
        self._write_session_report(now)

    def _tick(self, now: datetime) -> None:
        url = self._teacher_stream_url()
        has_audio = False
        has_video = False
        if url:
            has_audio, has_video = self.backend.probe_av(url, timeout_sec=3.0)
        has_av = has_audio and has_video

        if has_av:
            self._recover_if_needed(now)
            if self._active_proc is None:
                self._start_capture(url, now)
                return

            if url != self._active_url:
                self._log(
                    f"[recording] teacher stream url changed, switching source: {self._active_url} -> {url}"
                )
                self._stop_active_capture(now)
                self._start_capture(url, now)
                return

            if self._is_capture_stalled():
                self._log("[recording] capture stalled over max-lag threshold, restarting source")
                self._stop_active_capture(now)
                self._start_interruption(now, "capture_stalled")
                return
            return

        self._stop_active_capture(now)
        self._start_interruption(now, "stream_missing_av")
        self._promote_gap_if_unrecoverable(now)

    def _open_new_segment(self, started_at: datetime) -> None:
        self._segment_index += 1
        tmp_dir = self.session_meta.session_dir / "_tmp" / f"segment_{self._segment_index:05d}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        self._segment = SegmentManifest(
            index=self._segment_index,
            started_at=started_at,
            tmp_dir=tmp_dir,
        )
        self._log(f"[recording] opened segment #{self._segment_index} at {format_local_ts(started_at)}")

    def _maybe_roll_segment(self, now: datetime) -> None:
        if self.config.segment_minutes <= 0:
            return
        if self._segment is None:
            return
        segment_span = timedelta(minutes=max(1, self.config.segment_minutes))
        if now < self._segment.started_at + segment_span:
            return

        if self._interruption_started_at is not None and self._gap_open_started_at is None:
            duration = (now - self._interruption_started_at).total_seconds()
            if duration <= self.config.recovery_window_sec:
                return
            self._gap_open_started_at = self._interruption_started_at

        boundary = self._segment.started_at + segment_span
        self._stop_active_capture(boundary)
        self._flush_open_gap_if_needed(boundary, finalizing=False)
        self._finalize_current_segment(boundary)
        self._open_new_segment(boundary)

        if self._interruption_started_at is not None and self._gap_open_started_at is not None:
            self._interruption_started_at = boundary
            self._gap_open_started_at = boundary

    def _teacher_stream_url(self) -> str:
        snap = self.poller.get_snapshot()
        stream = snap.streams.get("teacher")
        if not stream:
            return ""
        return str(stream.stream_m3u8 or "").strip()

    def _start_capture(self, stream_url: str, now: datetime) -> None:
        if self._segment is None:
            return
        clip_name = f"clip_{len(self._segment.parts):05d}_{format_local_ts(now)}.writing.ts"
        clip_path = self._segment.tmp_dir / clip_name
        try:
            proc = self.backend.start_capture(stream_url, clip_path)
        except Exception as exc:
            self._log(f"[recording] failed to start ffmpeg capture: {exc}")
            self._start_interruption(now, "start_capture_failed")
            return

        self._active_proc = proc
        self._active_path = clip_path
        self._active_start_at = now
        self._active_url = stream_url
        self._active_last_size = 0
        self._active_last_growth_mono = time.monotonic()
        self._log(
            f"[recording] capture started for segment #{self._segment.index} "
            f"({format_local_ts(now)})"
        )

    def _stop_active_capture(self, now: datetime) -> None:
        if self._active_proc is None:
            return

        proc = self._active_proc
        path = self._active_path
        started_at = self._active_start_at

        self._active_proc = None
        self._active_path = None
        self._active_start_at = None
        self._active_url = ""
        self._active_last_size = 0
        self._active_last_growth_mono = 0.0

        try:
            self.backend.stop_capture(proc)
        except Exception:
            pass

        if self._segment is None or path is None or started_at is None:
            return

        final_path = path.with_name(path.name.replace(".writing", ""))
        try:
            if path.exists() and path.stat().st_size > 0:
                os.replace(path, final_path)
            elif final_path.exists() and final_path.stat().st_size > 0:
                pass
            else:
                if path.exists():
                    path.unlink(missing_ok=True)
                return
        except OSError:
            return

        self._segment.parts.append(
            SegmentPart(
                part_type="clip",
                started_at=started_at,
                ended_at=now,
                source_path=final_path,
            )
        )

    def _is_capture_stalled(self) -> bool:
        if self._active_proc is None or self._active_path is None:
            return False
        if self._active_proc.poll() is not None:
            return True

        path = self._active_path
        now_mono = time.monotonic()
        if path.exists():
            size = path.stat().st_size
            if size > self._active_last_size:
                self._active_last_size = size
                self._active_last_growth_mono = now_mono
                return False
        return (now_mono - self._active_last_growth_mono) > self.config.max_lag_sec

    def _start_interruption(self, now: datetime, reason: str) -> None:
        if self._interruption_started_at is None:
            self._interruption_started_at = now
            self._interruption_reason = reason
            self._log(
                f"[recording] interruption started at {format_local_ts(now)} reason={reason}; "
                f"retry window={self.config.recovery_window_sec:.0f}s"
            )

    def _promote_gap_if_unrecoverable(self, now: datetime) -> None:
        if self._interruption_started_at is None or self._gap_open_started_at is not None:
            return
        duration = (now - self._interruption_started_at).total_seconds()
        if duration > self.config.recovery_window_sec:
            self._gap_open_started_at = self._interruption_started_at
            self._log(
                f"[recording] interruption exceeded {self.config.recovery_window_sec:.0f}s; "
                "will render black/silent filler for missing interval"
            )

    def _recover_if_needed(self, now: datetime) -> None:
        if self._interruption_started_at is None:
            return

        duration = (now - self._interruption_started_at).total_seconds()
        if self._gap_open_started_at is not None or duration > self.config.recovery_window_sec:
            started_at = self._gap_open_started_at or self._interruption_started_at
            self._append_gap(started_at, now, self._interruption_reason or "unrecoverable_gap")
            self._log(
                f"[recording] interruption recovered after {duration:.1f}s; "
                "gap has been recorded for filler rendering"
            )
        else:
            self._log(f"[recording] interruption recovered within {duration:.1f}s (no filler needed)")

        self._interruption_started_at = None
        self._interruption_reason = ""
        self._gap_open_started_at = None

    def _append_gap(self, started_at: datetime, ended_at: datetime, reason: str) -> None:
        if self._segment is None:
            return
        if ended_at <= started_at:
            return
        gap = GapEvent(started_at=started_at, ended_at=ended_at, reason=reason)
        self._segment.gaps.append(gap)
        self._segment.parts.append(
            SegmentPart(
                part_type="gap",
                started_at=started_at,
                ended_at=ended_at,
                reason=reason,
            )
        )

    def _flush_open_gap_if_needed(self, now: datetime, *, finalizing: bool) -> None:
        if self._interruption_started_at is None:
            return
        duration = (now - self._interruption_started_at).total_seconds()
        if self._gap_open_started_at is None and duration <= self.config.recovery_window_sec and not finalizing:
            return
        started_at = self._gap_open_started_at or self._interruption_started_at
        self._append_gap(started_at, now, self._interruption_reason or "unrecoverable_gap")
        self._gap_open_started_at = now

    def _finalize_current_segment(self, ended_at: datetime) -> None:
        if self._segment is None:
            return
        self._segment.ended_at = ended_at

        seg = self._segment
        start_str = format_local_ts(seg.started_at)
        end_str = format_local_ts(ended_at)
        basename = f"{self._file_prefix}_{start_str}_{end_str}"

        final_mp4 = self.session_meta.session_dir / f"{basename}.mp4"
        final_mp3 = self.session_meta.session_dir / f"{basename}.mp3"
        writing_mp4 = self.session_meta.session_dir / f"{basename}.writing.mp4"
        writing_mp3 = self.session_meta.session_dir / f"{basename}.writing.mp3"
        missing_log = self.session_meta.session_dir / f"{basename}.missing.json"

        part_inputs: list[Path] = []
        gap_index = 0
        for idx, part in enumerate(seg.parts):
            if part.part_type == "clip":
                if part.source_path and part.source_path.exists() and part.source_path.stat().st_size > 0:
                    part_inputs.append(part.source_path)
                continue

            render_path = seg.tmp_dir / f"gap_{idx:05d}.ts"
            rendered = self.backend.render_gap_clip(part.duration_sec, render_path)
            if rendered:
                part.rendered_path = render_path
                part_inputs.append(render_path)
                if gap_index < len(seg.gaps):
                    seg.gaps[gap_index].rendered = True
            gap_index += 1

        mp4_ok = False
        mp3_ok = False
        if part_inputs:
            try:
                mp4_ok = self.backend.finalize_segment(
                    part_inputs,
                    writing_mp4,
                    prefer_copy=not seg.has_gaps,
                )
                if mp4_ok:
                    os.replace(writing_mp4, final_mp4)
                    mp3_ok = self.backend.export_mp3(final_mp4, writing_mp3)
                    if mp3_ok:
                        os.replace(writing_mp3, final_mp3)
                    else:
                        writing_mp3.unlink(missing_ok=True)
                else:
                    writing_mp4.unlink(missing_ok=True)
            except Exception as exc:
                self._log(f"[recording] finalize failed for segment #{seg.index}: {exc}")
                writing_mp4.unlink(missing_ok=True)
                writing_mp3.unlink(missing_ok=True)

        segment_summary = {
            "index": seg.index,
            "started_at_local": start_str,
            "ended_at_local": end_str,
            "mp4_path": str(final_mp4),
            "mp3_path": str(final_mp3),
            "mp4_ok": mp4_ok,
            "mp3_ok": mp3_ok,
            "has_gaps": seg.has_gaps,
            "gap_count": len(seg.gaps),
            "gaps": [gap.to_json_dict() for gap in seg.gaps],
            "parts": [part.to_json_dict() for part in seg.parts],
        }
        with missing_log.open("w", encoding="utf-8") as handle:
            json.dump(segment_summary, handle, ensure_ascii=False, indent=2)

        self._segment_outputs.append(segment_summary)
        self._log(
            f"[recording] finalized segment #{seg.index} "
            f"({start_str} -> {end_str}) mp4_ok={mp4_ok} mp3_ok={mp3_ok} gaps={len(seg.gaps)}"
        )

        try:
            shutil.rmtree(seg.tmp_dir, ignore_errors=True)
        except OSError:
            pass

        self._segment = None

    def _write_session_report(self, ended_at: datetime) -> None:
        missing_ranges: list[dict] = []
        for segment in self._segment_outputs:
            for gap in segment.get("gaps", []):
                missing_ranges.append(gap)

        report = {
            "course_title": self.session_meta.course_title,
            "teacher_name": self.session_meta.teacher_name,
            "session_dir": str(self.session_meta.session_dir),
            "watch_started_at_local": format_local_ts(self.session_meta.watch_started_at),
            "watch_ended_at_local": format_local_ts(ended_at),
            "segment_count": len(self._segment_outputs),
            "missing_interval_count": len(missing_ranges),
            "missing_intervals": missing_ranges,
            "segments": self._segment_outputs,
        }
        report_path = self.session_meta.session_dir / "recording_session_report.json"
        with report_path.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        self._log(f"[recording] session report generated: {report_path}")

    def _now(self) -> datetime:
        return datetime.now().astimezone()

    def _log(self, msg: str) -> None:
        with self._lock:
            self._log_fn(msg)
