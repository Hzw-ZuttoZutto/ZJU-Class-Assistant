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
    def __init__(self, url: str, *, stream_play: str = "") -> None:
        self.stream_m3u8 = url
        self.stream_play = stream_play


class _FakeSnapshot:
    def __init__(
        self,
        url: str,
        *,
        active_provider: str = "",
        stream_play: str = "",
        class_url: str = "",
        class_stream_play: str = "",
    ) -> None:
        streams = {}
        if url or stream_play:
            streams["teacher"] = _FakeStream(url, stream_play=stream_play)
        if class_url or class_stream_play:
            streams["class"] = _FakeStream(class_url, stream_play=class_stream_play)
        self.streams = streams
        self.active_provider = active_provider


class _FakePoller:
    def __init__(
        self,
        url: str,
        *,
        active_provider: str = "",
        stream_play: str = "",
        class_url: str = "",
        class_stream_play: str = "",
    ) -> None:
        self.url = url
        self.active_provider = active_provider
        self.stream_play = stream_play
        self.class_url = class_url
        self.class_stream_play = class_stream_play

    def get_snapshot(self):
        return _FakeSnapshot(
            self.url,
            active_provider=self.active_provider,
            stream_play=self.stream_play,
            class_url=self.class_url,
            class_stream_play=self.class_stream_play,
        )


class _RtcOnlyBackend(_FakeBackend):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def probe_audio(self, stream_url: str, *, timeout_sec: float = 4.0) -> bool:  # noqa: ARG002
        self.calls.append(stream_url)
        return stream_url.startswith("webrtc://")


class _ClassOnlyBackend(_FakeBackend):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def probe_audio(self, stream_url: str, *, timeout_sec: float = 4.0) -> bool:  # noqa: ARG002
        self.calls.append(stream_url)
        return "class" in stream_url


class _TimeoutAwareBackend(_FakeBackend):
    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    def probe_audio(self, stream_url: str, *, timeout_sec: float = 4.0) -> bool:
        timeout = float(timeout_sec)
        self.calls.append((stream_url, timeout))
        if stream_url.startswith("webrtc://"):
            return timeout >= 8.5
        return bool(stream_url)


class _HlsOnlyBackend(_FakeBackend):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def probe_audio(self, stream_url: str, *, timeout_sec: float = 4.0) -> bool:  # noqa: ARG002
        self.calls.append(stream_url)
        return stream_url.endswith(".m3u8")


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

    def test_startup_check_prefers_rtc_candidate_before_hls(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backend = _RtcOnlyBackend()
            service = AudioOnlyRecorderService(
                poller=_FakePoller(
                    "https://example.test/live.m3u8",
                    stream_play="webrtc://rtc.zju.edu.cn/live/teacher?vhost=video",
                ),
                config=AudioRecordingConfig(poll_interval_sec=0.2, max_lag_sec=999.0),
                session_meta=AudioSessionMeta(
                    course_title="课程A",
                    teacher_name="老师A",
                    session_dir=root,
                    started_at=datetime.now(timezone.utc),
                ),
                backend=backend,
                log_fn=lambda _msg: None,
            )
            ok, err = service.startup_check(timeout_sec=1.0)
            self.assertTrue(ok)
            self.assertEqual(err, "")
            self.assertGreaterEqual(len(backend.calls), 1)
            self.assertEqual(backend.calls[0], "webrtc://rtc.zju.edu.cn/live/teacher?vhost=video")

    def test_startup_check_prefers_stable_hls_for_livingroom_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backend = _HlsOnlyBackend()
            service = AudioOnlyRecorderService(
                poller=_FakePoller(
                    "https://example.test/live.m3u8",
                    active_provider="livingroom",
                    stream_play="webrtc://rtc.zju.edu.cn/live/teacher?vhost=video",
                ),
                config=AudioRecordingConfig(poll_interval_sec=0.2, max_lag_sec=999.0),
                session_meta=AudioSessionMeta(
                    course_title="课程A",
                    teacher_name="老师A",
                    session_dir=root,
                    started_at=datetime.now(timezone.utc),
                ),
                backend=backend,
                log_fn=lambda _msg: None,
            )
            ok, err = service.startup_check(timeout_sec=1.0)
            self.assertTrue(ok)
            self.assertEqual(err, "")
            self.assertGreaterEqual(len(backend.calls), 1)
            self.assertEqual(backend.calls[0], "https://example.test/live.m3u8")

    def test_startup_check_falls_back_to_class_sources_after_teacher(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backend = _ClassOnlyBackend()
            service = AudioOnlyRecorderService(
                poller=_FakePoller(
                    "https://example.test/teacher.m3u8",
                    stream_play="webrtc://rtc.zju.edu.cn/live/teacher?vhost=video",
                    class_url="https://example.test/class.m3u8",
                    class_stream_play="webrtc://rtc.zju.edu.cn/live/class?vhost=video",
                ),
                config=AudioRecordingConfig(poll_interval_sec=0.2, max_lag_sec=999.0),
                session_meta=AudioSessionMeta(
                    course_title="课程A",
                    teacher_name="老师A",
                    session_dir=root,
                    started_at=datetime.now(timezone.utc),
                ),
                backend=backend,
                log_fn=lambda _msg: None,
            )
            ok, err = service.startup_check(timeout_sec=1.0)
            self.assertTrue(ok)
            self.assertEqual(err, "")
            self.assertGreaterEqual(len(backend.calls), 3)
            self.assertEqual(backend.calls[0], "webrtc://rtc.zju.edu.cn/live/teacher?vhost=video")
            self.assertEqual(backend.calls[1], "https://example.test/teacher.m3u8")
            self.assertEqual(backend.calls[2], "webrtc://rtc.zju.edu.cn/live/class?vhost=video")

    def test_startup_check_uses_protocol_specific_probe_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            backend = _TimeoutAwareBackend()
            service = AudioOnlyRecorderService(
                poller=_FakePoller(
                    "https://example.test/teacher.m3u8",
                    stream_play="webrtc://rtc.zju.edu.cn/live/teacher?vhost=video",
                ),
                config=AudioRecordingConfig(poll_interval_sec=0.2, max_lag_sec=999.0),
                session_meta=AudioSessionMeta(
                    course_title="课程A",
                    teacher_name="老师A",
                    session_dir=root,
                    started_at=datetime.now(timezone.utc),
                ),
                backend=backend,
                log_fn=lambda _msg: None,
            )
            ok, err = service.startup_check(timeout_sec=1.0)
            self.assertTrue(ok)
            self.assertEqual(err, "")
            self.assertGreaterEqual(len(backend.calls), 2)
            self.assertEqual(
                backend.calls[0],
                ("webrtc://rtc.zju.edu.cn/live/teacher?vhost=video", 8.0),
            )
            self.assertEqual(backend.calls[1], ("https://example.test/teacher.m3u8", 3.0))


if __name__ == "__main__":
    unittest.main()
