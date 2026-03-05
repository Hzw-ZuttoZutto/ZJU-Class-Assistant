from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.live.recording.models import RecordingConfig, SegmentPart, SessionMeta
from src.live.recording.service import LiveRecorderService


class _FakeStream:
    def __init__(self, url: str) -> None:
        self.stream_m3u8 = url


class _FakeSnapshot:
    def __init__(self, url: str) -> None:
        self.streams = {"teacher": _FakeStream(url)} if url else {}


class _FakePoller:
    def __init__(self, url: str) -> None:
        self.url = url

    def get_snapshot(self):
        return _FakeSnapshot(self.url)


class _FakeBackend:
    def __init__(self, probe_results: list[tuple[bool, bool]] | None = None) -> None:
        self.probe_results = list(probe_results or [(True, True)])

    def ensure_available(self) -> bool:
        return True

    def probe_av(self, url: str, timeout_sec: float = 5.0) -> tuple[bool, bool]:
        if self.probe_results:
            return self.probe_results.pop(0)
        return (False, False)

    def start_capture(self, stream_url: str, output_path: Path):
        raise RuntimeError("not used in this test")

    def stop_capture(self, proc) -> None:
        return

    def render_gap_clip(self, duration_sec: float, output_path: Path) -> bool:
        output_path.write_bytes(b"gap")
        return True

    def finalize_segment(self, input_parts: list[Path], output_mp4: Path, *, prefer_copy: bool) -> bool:
        output_mp4.write_bytes(b"mp4")
        return True

    def export_mp3(self, input_mp4: Path, output_mp3: Path) -> bool:
        output_mp3.write_bytes(b"mp3")
        return True


class _DummyProc:
    def __init__(self) -> None:
        self._stopped = False

    def poll(self):
        return 0 if self._stopped else None


class _SlowFinalizeBackend(_FakeBackend):
    def __init__(self, delay_sec: float) -> None:
        super().__init__([(True, True)])
        self.delay_sec = delay_sec

    def probe_av(self, url: str, timeout_sec: float = 5.0) -> tuple[bool, bool]:
        return (True, True)

    def start_capture(self, stream_url: str, output_path: Path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"clip")
        return _DummyProc()

    def stop_capture(self, proc) -> None:
        proc._stopped = True

    def finalize_segment(self, input_parts: list[Path], output_mp4: Path, *, prefer_copy: bool) -> bool:
        time.sleep(self.delay_sec)
        output_mp4.write_bytes(b"mp4")
        return True


class _FailFinalizeBackend(_FakeBackend):
    def finalize_segment(self, input_parts: list[Path], output_mp4: Path, *, prefer_copy: bool) -> bool:
        output_mp4.write_bytes(b"partial")
        return False


def _build_service(tmp_dir: Path, backend: _FakeBackend) -> LiveRecorderService:
    now = datetime.now(timezone.utc)
    return LiveRecorderService(
        poller=_FakePoller("https://x.zju.edu.cn/live.m3u8"),
        config=RecordingConfig(
            root_dir=tmp_dir,
            segment_minutes=10,
            startup_av_timeout=15.0,
            recovery_window_sec=10.0,
        ),
        session_meta=SessionMeta(
            course_title="课程A",
            teacher_name="老师B",
            watch_started_at=now,
            session_dir=tmp_dir,
        ),
        backend=backend,
        log_fn=lambda _: None,
    )


class RecordingServiceTests(unittest.TestCase):
    def test_startup_check_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            service = _build_service(Path(td), _FakeBackend([(False, True), (False, True)]))
            ok, msg = service.startup_check(timeout_sec=1.0)
            self.assertFalse(ok)
            self.assertIn("not detected", msg)

    def test_startup_check_success(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            service = _build_service(Path(td), _FakeBackend([(True, True)]))
            ok, msg = service.startup_check(timeout_sec=1.0)
            self.assertTrue(ok)
            self.assertEqual(msg, "")

    def test_unrecoverable_gap_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            service = _build_service(Path(td), _FakeBackend())
            start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
            service._open_new_segment(start)
            service._interruption_started_at = start
            service._interruption_reason = "stream_missing_av"
            service._promote_gap_if_unrecoverable(start + timedelta(seconds=11))
            service._flush_open_gap_if_needed(start + timedelta(seconds=20), finalizing=False)
            self.assertIsNotNone(service._segment)
            self.assertEqual(len(service._segment.gaps), 1)
            self.assertGreaterEqual(service._segment.gaps[0].duration_sec, 20.0)

    def test_finalize_segment_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            service = _build_service(tmp_path, _FakeBackend())
            start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
            end = start + timedelta(seconds=10)
            service._open_new_segment(start)
            clip = tmp_path / "_tmp" / "segment_00001" / "clip.ts"
            clip.parent.mkdir(parents=True, exist_ok=True)
            clip.write_bytes(b"clip")
            service._segment.parts.append(
                SegmentPart(
                    part_type="clip",
                    started_at=start,
                    ended_at=end,
                    source_path=clip,
                )
            )
            service._finalize_current_segment(end)

            mp4_files = sorted(tmp_path.glob("*.mp4"))
            mp3_files = sorted(tmp_path.glob("*.mp3"))
            missing_logs = sorted(tmp_path.glob("*.missing.json"))

            self.assertEqual(len(mp4_files), 1)
            self.assertEqual(len(mp3_files), 1)
            self.assertEqual(len(missing_logs), 1)

            payload = json.loads(missing_logs[0].read_text(encoding="utf-8"))
            self.assertTrue(payload["mp4_ok"])
            self.assertTrue(payload["mp3_ok"])
            self.assertEqual(payload["gap_count"], 0)

    def test_stop_waits_for_finalize_and_no_writing_left(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            service = _build_service(tmp_path, _SlowFinalizeBackend(delay_sec=8.2))

            service.start()
            time.sleep(0.4)

            stop_started = time.monotonic()
            service.stop()
            stop_elapsed = time.monotonic() - stop_started

            self.assertGreaterEqual(stop_elapsed, 8.0)
            self.assertTrue((tmp_path / "recording_session_report.json").exists())
            self.assertEqual(list(tmp_path.glob("*.writing.mp4")), [])

    def test_finalize_failure_cleans_writing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            service = _build_service(tmp_path, _FailFinalizeBackend())
            start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
            end = start + timedelta(seconds=5)

            service._open_new_segment(start)
            clip = tmp_path / "_tmp" / "segment_00001" / "clip.ts"
            clip.parent.mkdir(parents=True, exist_ok=True)
            clip.write_bytes(b"clip")
            service._segment.parts.append(
                SegmentPart(
                    part_type="clip",
                    started_at=start,
                    ended_at=end,
                    source_path=clip,
                )
            )
            service._finalize_current_segment(end)

            self.assertEqual(list(tmp_path.glob("*.writing.mp4")), [])
            self.assertEqual(list(tmp_path.glob("*.writing.mp3")), [])


if __name__ == "__main__":
    unittest.main()
