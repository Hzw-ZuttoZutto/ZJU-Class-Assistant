from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.live.tingwu.audio_recorder import (
    AudioOnlyRecorderService,
    AudioRecordingConfig,
    AudioSessionMeta,
)


class _FakeProc:
    def __init__(self) -> None:
        self._stopped = False

    def poll(self):
        return 0 if self._stopped else None

    def send_signal(self, _sig) -> None:
        self._stopped = True

    def wait(self, timeout: float = 1.0) -> int:  # noqa: ARG002
        self._stopped = True
        return 0

    def kill(self) -> None:
        self._stopped = True


class _FakeBackend:
    def ensure_available(self) -> bool:
        return True

    def probe_audio(self, stream_url: str, *, timeout_sec: float = 4.0) -> bool:  # noqa: ARG002
        return bool(stream_url)

    def start_capture(self, stream_url: str, output_path: Path):  # noqa: ARG002
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"segment")
        return _FakeProc()

    def stop_capture(self, proc, *, grace_sec: float = 3.0) -> None:  # noqa: ARG002
        proc._stopped = True

    def merge_mp3_segments(self, segments: list[Path], output_mp3: Path) -> bool:
        _ = segments
        output_mp3.write_bytes(b"merged")
        return True


class _FakeStream:
    def __init__(self, url: str) -> None:
        self.stream_m3u8 = url


class _FakeSnapshot:
    def __init__(self, url: str) -> None:
        self.streams = {"teacher": _FakeStream(url)}


class _FakePoller:
    def __init__(self, url: str) -> None:
        self.url = url

    def get_snapshot(self):
        return _FakeSnapshot(self.url)


class TingwuAudioRecorderTests(unittest.TestCase):
    def test_audio_recorder_generates_final_mp3_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            service = AudioOnlyRecorderService(
                poller=_FakePoller("https://example.test/live.m3u8"),
                config=AudioRecordingConfig(poll_interval_sec=0.2, max_lag_sec=999.0),
                session_meta=AudioSessionMeta(
                    course_title="课程A",
                    teacher_name="老师A",
                    session_dir=root,
                    started_at=datetime.now(timezone.utc),
                ),
                backend=_FakeBackend(),
                log_fn=lambda _msg: None,
            )
            ok, err = service.startup_check(timeout_sec=1.0)
            self.assertTrue(ok)
            self.assertEqual(err, "")
            service.start()
            time.sleep(0.4)
            result = service.stop()

            self.assertTrue(result.success)
            self.assertIsNotNone(result.final_mp3_path)
            assert result.final_mp3_path is not None
            self.assertTrue(result.final_mp3_path.exists())
            self.assertTrue(result.report_path.exists())
            self.assertGreaterEqual(result.segment_count, 1)

    def test_stop_without_start_writes_failure_report(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            service = AudioOnlyRecorderService(
                poller=_FakePoller(""),
                config=AudioRecordingConfig(poll_interval_sec=0.2, max_lag_sec=1.0),
                session_meta=AudioSessionMeta(
                    course_title="课程A",
                    teacher_name="老师A",
                    session_dir=root,
                    started_at=datetime.now(timezone.utc),
                ),
                backend=_FakeBackend(),
                log_fn=lambda _msg: None,
            )
            result = service.stop()
            self.assertFalse(result.success)
            self.assertTrue(result.report_path.exists())


if __name__ == "__main__":
    unittest.main()
