from __future__ import annotations

import argparse
import unittest
from unittest import mock

from src.common.course_meta import CourseMeta
from src.live.server import run_watch


def _watch_args() -> argparse.Namespace:
    return argparse.Namespace(
        username="u",
        password="p",
        tenant_code="112",
        authcode="",
        timeout=5,
        course_id=1,
        sub_id=2,
        poll_interval=3.0,
        host="127.0.0.1",
        port=8765,
        open_base_url="",
        no_browser=True,
        playlist_retries=1,
        asset_retries=1,
        stale_playlist_grace=15.0,
        hls_max_buffer=20,
        record_dir="",
        record_segment_minutes=10,
        record_startup_av_timeout=1.0,
        record_recovery_window_sec=10.0,
        rt_insight_enabled=False,
        rt_chunk_seconds=10,
        rt_context_window_seconds=180,
        rt_model="gpt-5-mini",
        rt_stt_model="whisper-large-v3",
        rt_keywords_file="config/realtime_keywords.json",
        rt_api_base_url="",
        rt_stt_request_timeout_sec=8.0,
        rt_stt_stage_timeout_sec=32.0,
        rt_stt_retry_count=4,
        rt_stt_retry_interval_sec=0.2,
        rt_analysis_request_timeout_sec=15.0,
        rt_analysis_stage_timeout_sec=60.0,
        rt_analysis_retry_count=4,
        rt_analysis_retry_interval_sec=0.2,
        rt_alert_threshold=90,
        rt_dingtalk_enabled=False,
        rt_dingtalk_cooldown_sec=30.0,
        rt_max_concurrency=5,
        rt_context_min_ready=15,
        rt_context_recent_required=4,
        rt_context_wait_timeout_sec_1=1.0,
        rt_context_wait_timeout_sec_2=5.0,
    )


class _JoinResult:
    attempted = False
    success = False
    message = ""
    stream_id = ""


class _FakePoller:
    instance = None

    def __init__(self, **kwargs) -> None:
        self.started = False
        self.stopped = False
        _FakePoller.instance = self

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class _FakeRecorder:
    instances = []
    startup_ok = True

    @staticmethod
    def build_session_dir(*, record_dir: str | None, course_title: str, teacher_name: str, started_at):
        import pathlib

        base = pathlib.Path(record_dir) if record_dir else pathlib.Path.cwd()
        return base / "fake_session"

    def __init__(self, **kwargs) -> None:
        self.started = False
        self.stopped = False
        _FakeRecorder.instances.append(self)

    def startup_check(self, timeout_sec: float):
        if _FakeRecorder.startup_ok:
            return True, ""
        return False, "no av"

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class _FakeServer:
    instance = None

    def __init__(self, *args, **kwargs) -> None:
        self.closed = False
        _FakeServer.instance = self

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        raise KeyboardInterrupt()

    def server_close(self) -> None:
        self.closed = True


class _FakeInsightService:
    instances = []

    def __init__(self, **kwargs) -> None:
        self.started = False
        self.stopped = False
        _FakeInsightService.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class WatchRecordingTests(unittest.TestCase):
    def test_watch_fails_when_course_meta_missing(self) -> None:
        args = _watch_args()
        with (
            mock.patch("src.live.server.ZJUAuthClient.login_and_get_token", return_value="tok"),
            mock.patch("src.live.server.fetch_course_meta", return_value=None),
        ):
            code = run_watch(args)
        self.assertEqual(code, 1)

    def test_watch_fails_on_startup_av_check(self) -> None:
        args = _watch_args()
        _FakeRecorder.instances.clear()
        _FakeRecorder.startup_ok = False

        with (
            mock.patch("src.live.server.ZJUAuthClient.login_and_get_token", return_value="tok"),
            mock.patch(
                "src.live.server.fetch_course_meta",
                return_value=CourseMeta(course_id=1, title="课程", teachers=["老师"]),
            ),
            mock.patch("src.live.server.JoinRoomClient.try_join", return_value=_JoinResult()),
            mock.patch("src.live.server.StreamPoller", _FakePoller),
            mock.patch("src.live.server.LiveRecorderService", _FakeRecorder),
        ):
            code = run_watch(args)

        self.assertEqual(code, 1)
        self.assertTrue(_FakePoller.instance.stopped)
        self.assertFalse(_FakeRecorder.instances[0].started)

    def test_watch_stops_recorder_with_server(self) -> None:
        args = _watch_args()
        args.rt_insight_enabled = True
        _FakeRecorder.instances.clear()
        _FakeRecorder.startup_ok = True
        _FakeInsightService.instances.clear()

        with (
            mock.patch("src.live.server.ZJUAuthClient.login_and_get_token", return_value="tok"),
            mock.patch(
                "src.live.server.fetch_course_meta",
                return_value=CourseMeta(course_id=1, title="课程", teachers=["老师"]),
            ),
            mock.patch("src.live.server.JoinRoomClient.try_join", return_value=_JoinResult()),
            mock.patch("src.live.server.StreamPoller", _FakePoller),
            mock.patch("src.live.server.LiveRecorderService", _FakeRecorder),
            mock.patch("src.live.server.RealtimeInsightService", _FakeInsightService),
            mock.patch("src.live.server.ProxyEngine"),
            mock.patch("src.live.server.prepare_hls_js", return_value=""),
            mock.patch("src.live.server.ThreadingHTTPServer", _FakeServer),
        ):
            code = run_watch(args)

        self.assertEqual(code, 0)
        self.assertTrue(_FakePoller.instance.stopped)
        self.assertTrue(_FakeRecorder.instances[0].started)
        self.assertTrue(_FakeRecorder.instances[0].stopped)
        self.assertTrue(_FakeInsightService.instances[0].started)
        self.assertTrue(_FakeInsightService.instances[0].stopped)
        self.assertTrue(_FakeServer.instance.closed)


if __name__ == "__main__":
    unittest.main()
