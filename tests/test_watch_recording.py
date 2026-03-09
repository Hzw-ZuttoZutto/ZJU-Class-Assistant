from __future__ import annotations

import argparse
import unittest
from unittest import mock

from src.common.course_meta import CourseMeta
from src.live.analysis import run_analysis


def _analysis_args() -> argparse.Namespace:
    return argparse.Namespace(
        username="u",
        password="p",
        tenant_code="112",
        authcode="",
        timeout=5,
        course_id=1,
        sub_id=2,
        poll_interval=3.0,
        output_dir="",
        rt_model="gpt-5-mini",
        rt_asr_scene="zh",
        rt_asr_model="paraformer-realtime-v2",
        rt_hotwords_file="config/realtime_hotwords.json",
        rt_window_sentences=8,
        rt_stream_analysis_workers=32,
        rt_stream_queue_size=100,
        rt_asr_endpoint="wss://dashscope.aliyuncs.com/api-ws/v1/inference",
        rt_translation_target_languages="zh",
        rt_keywords_file="config/realtime_keywords.json",
        rt_api_base_url="",
        rt_analysis_request_timeout_sec=15.0,
        rt_analysis_stage_timeout_sec=60.0,
        rt_analysis_retry_count=4,
        rt_analysis_retry_interval_sec=0.2,
        rt_alert_threshold=90,
        rt_dingtalk_enabled=True,
        rt_dingtalk_cooldown_sec=30.0,
        rt_dingtalk_queue_size=500,
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
        del kwargs
        self.started = False
        self.stopped = False
        self.start_calls = 0
        self.stop_calls = 0
        _FakePoller.instance = self

    def start(self) -> None:
        self.start_calls += 1
        self.started = True
        self.stopped = False

    def stop(self) -> None:
        self.stop_calls += 1
        self.stopped = True

    def is_running(self) -> bool:
        return bool(self.started and not self.stopped)


class _FakeInsightService:
    instances = []

    def __init__(self, **kwargs) -> None:
        self.started = False
        self.stopped = False
        self.start_calls = 0
        self.stop_calls = 0
        self.notifier = kwargs.get("notifier")
        _FakeInsightService.instances.append(self)

    def start(self) -> None:
        self.start_calls += 1
        self.started = True
        self.stopped = False

    def stop(self) -> None:
        self.stop_calls += 1
        self.stopped = True

    def is_running(self) -> bool:
        return bool(self.started and not self.stopped)


class AnalysisModeTests(unittest.TestCase):
    def test_analysis_requires_explicit_asr_model(self) -> None:
        args = _analysis_args()
        args.rt_asr_model = None
        code = run_analysis(args)
        self.assertEqual(code, 1)

    def test_analysis_requires_valid_hotwords_file(self) -> None:
        args = _analysis_args()
        args.rt_hotwords_file = "/tmp/not_found_hotwords.json"
        code = run_analysis(args)
        self.assertEqual(code, 1)

    def test_analysis_rejects_invalid_dingtalk_queue_size(self) -> None:
        args = _analysis_args()
        args.rt_dingtalk_queue_size = 0
        code = run_analysis(args)
        self.assertEqual(code, 1)

    def test_analysis_fails_when_course_meta_missing(self) -> None:
        args = _analysis_args()
        with (
            mock.patch("src.live.analysis.ZJUAuthClient.login_and_get_token", return_value="tok"),
            mock.patch("src.live.analysis.fetch_course_meta", return_value=None),
        ):
            code = run_analysis(args)
        self.assertEqual(code, 1)

    def test_analysis_starts_and_stops_with_server_loop(self) -> None:
        args = _analysis_args()
        _FakePoller.instance = None
        _FakeInsightService.instances.clear()

        with (
            mock.patch("src.live.analysis.ZJUAuthClient.login_and_get_token", return_value="tok"),
            mock.patch(
                "src.live.analysis.fetch_course_meta",
                return_value=CourseMeta(course_id=1, title="课程", teachers=["老师"]),
            ),
            mock.patch("src.live.analysis.JoinRoomClient.try_join", return_value=_JoinResult()),
            mock.patch("src.live.analysis.StreamPoller", _FakePoller),
            mock.patch("src.live.analysis.RealtimeInsightService", _FakeInsightService),
            mock.patch(
                "src.live.analysis.resolve_dingtalk_bot_settings",
                return_value=("https://example.test/robot/send?access_token=x", "secret", ""),
            ),
            mock.patch("src.live.analysis.time.sleep", side_effect=KeyboardInterrupt),
        ):
            code = run_analysis(args)

        self.assertEqual(code, 0)
        self.assertTrue(_FakePoller.instance.started)
        self.assertTrue(_FakePoller.instance.stopped)
        self.assertTrue(_FakeInsightService.instances[0].started)
        self.assertTrue(_FakeInsightService.instances[0].stopped)

    def test_analysis_requires_dingtalk_enabled(self) -> None:
        args = _analysis_args()
        args.rt_dingtalk_enabled = False
        code = run_analysis(args)
        self.assertEqual(code, 1)

    def test_analysis_stream_uses_dingtalk_notifier(self) -> None:
        args = _analysis_args()
        _FakePoller.instance = None
        _FakeInsightService.instances.clear()

        with (
            mock.patch("src.live.analysis.ZJUAuthClient.login_and_get_token", return_value="tok"),
            mock.patch(
                "src.live.analysis.fetch_course_meta",
                return_value=CourseMeta(course_id=1, title="课程", teachers=["老师"]),
            ),
            mock.patch("src.live.analysis.JoinRoomClient.try_join", return_value=_JoinResult()),
            mock.patch("src.live.analysis.StreamPoller", _FakePoller),
            mock.patch("src.live.analysis.RealtimeInsightService", _FakeInsightService),
            mock.patch(
                "src.live.analysis.resolve_dingtalk_bot_settings",
                return_value=("https://example.test/robot/send?access_token=x", "secret", ""),
            ),
            mock.patch("src.live.analysis.time.sleep", side_effect=KeyboardInterrupt),
        ):
            code = run_analysis(args)

        self.assertEqual(code, 0)
        self.assertIsNotNone(_FakeInsightService.instances[0].notifier)

    def test_analysis_watchdog_restarts_dead_components(self) -> None:
        args = _analysis_args()
        _FakePoller.instance = None
        _FakeInsightService.instances.clear()
        sleep_calls = {"count": 0}

        def _fake_sleep(_seconds: float) -> None:
            sleep_calls["count"] += 1
            if sleep_calls["count"] == 1:
                if _FakePoller.instance is not None:
                    _FakePoller.instance.stopped = True
                if _FakeInsightService.instances:
                    _FakeInsightService.instances[0].stopped = True
                return
            raise KeyboardInterrupt()

        with (
            mock.patch("src.live.analysis.ZJUAuthClient.login_and_get_token", return_value="tok"),
            mock.patch(
                "src.live.analysis.fetch_course_meta",
                return_value=CourseMeta(course_id=1, title="课程", teachers=["老师"]),
            ),
            mock.patch("src.live.analysis.JoinRoomClient.try_join", return_value=_JoinResult()),
            mock.patch("src.live.analysis.StreamPoller", _FakePoller),
            mock.patch("src.live.analysis.RealtimeInsightService", _FakeInsightService),
            mock.patch(
                "src.live.analysis.resolve_dingtalk_bot_settings",
                return_value=("https://example.test/robot/send?access_token=x", "secret", ""),
            ),
            mock.patch("src.live.analysis.time.sleep", side_effect=_fake_sleep),
        ):
            code = run_analysis(args)

        self.assertEqual(code, 0)
        self.assertIsNotNone(_FakePoller.instance)
        self.assertGreaterEqual(_FakePoller.instance.start_calls, 2)
        self.assertTrue(_FakePoller.instance.stopped)
        self.assertTrue(_FakeInsightService.instances)
        self.assertGreaterEqual(_FakeInsightService.instances[0].start_calls, 2)
        self.assertTrue(_FakeInsightService.instances[0].stopped)


if __name__ == "__main__":
    unittest.main()
