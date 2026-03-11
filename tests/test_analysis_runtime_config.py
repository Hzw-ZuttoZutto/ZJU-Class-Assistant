from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.common.course_meta import CourseMeta
from src.live.analysis import run_analysis


class _FakeTokenManager:
    def __init__(self, **_kwargs) -> None:
        self._token = "tok"

    def refresh(self, *_args, **_kwargs) -> tuple[bool, str]:
        return True, ""

    def get_token(self) -> str:
        return self._token


class _FakePoller:
    def __init__(self, **_kwargs) -> None:
        self._running = False

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def is_running(self) -> bool:
        return bool(self._running)

    def get_metrics(self) -> dict[str, object]:
        return {}


class _FakeInsightService:
    def __init__(self, **_kwargs) -> None:
        self._running = False

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def is_running(self) -> bool:
        return bool(self._running)

    def get_runtime_snapshot(self) -> dict[str, object]:
        return {
            "service_running": self._running,
            "stream_metrics": {},
            "stage_metrics": {},
        }


class _FakeRuntimeObserver:
    init_kwargs: dict[str, object] = {}

    def __init__(self, **kwargs) -> None:
        _FakeRuntimeObserver.init_kwargs = dict(kwargs)

    def observe(self, _snapshot: dict[str, object]) -> None:
        return

    def close(self) -> None:
        return

    def notify_watchdog_restart_failed(self, **_kwargs) -> None:
        return

    def notify_watchdog_recovery_pending(self, **_kwargs) -> None:
        return


class _FakeDingTalkNotifier:
    def __init__(self, **_kwargs) -> None:
        return

    def notify_event(self, *_args, **_kwargs) -> bool:
        return True

    def stop(self) -> None:
        return


class _JoinResult:
    attempted = False
    success = False
    message = ""
    stream_id = ""


def _analysis_args() -> argparse.Namespace:
    return argparse.Namespace(
        username="u",
        password="p",
        tenant_code="112",
        authcode="",
        timeout=20,
        course_id=1,
        sub_id=2,
        poll_interval=3.0,
        output_dir="",
        rt_model="gpt-4.1-mini",
        rt_asr_scene="zh",
        rt_asr_model="fun-asr-realtime",
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
        rt_log_rotate_max_bytes=64 * 1024 * 1024,
        rt_log_rotate_backup_count=20,
        tingwu_enabled=False,
        tingwu_poll_interval_sec=30.0,
        tingwu_max_wait_hours=6.0,
    )


class _FakeRecorderResult:
    def __init__(self, mp3_path: Path, report_path: Path, *, success: bool = True, error: str = "") -> None:
        self.final_mp3_path = mp3_path
        self.report_path = report_path
        self.success = success
        self.error = error
        self.segment_count = 1


class _FakeAudioRecorder:
    last_instance = None

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.session_dir = Path(kwargs["session_meta"].session_dir)
        self._started = False
        type(self).last_instance = self

    def startup_check(self, timeout_sec: float = 20.0) -> tuple[bool, str]:
        _ = timeout_sec
        return True, ""

    def start(self) -> None:
        self._started = True

    def stop(self) -> _FakeRecorderResult:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        mp3_path = self.session_dir / "tingwu_audio_full.mp3"
        report_path = self.session_dir / "tingwu_audio_recording_report.json"
        mp3_path.write_bytes(b"mp3")
        report_path.write_text("{}", encoding="utf-8")
        return _FakeRecorderResult(mp3_path=mp3_path, report_path=report_path, success=True)


class AnalysisRuntimeConfigTests(unittest.TestCase):
    def test_runtime_monitor_disables_data_stall_alert_by_default(self) -> None:
        args = _analysis_args()
        _FakeRuntimeObserver.init_kwargs = {}
        with tempfile.TemporaryDirectory() as td:
            args.output_dir = td
            with (
                mock.patch("src.live.analysis.LoginTokenManager", _FakeTokenManager),
                mock.patch("src.live.analysis.fetch_course_meta", return_value=CourseMeta(course_id=1, title="课程", teachers=["老师"])),
                mock.patch("src.live.analysis.JoinRoomClient.try_join", return_value=_JoinResult()),
                mock.patch("src.live.analysis.StreamPoller", _FakePoller),
                mock.patch("src.live.analysis.RealtimeInsightService", _FakeInsightService),
                mock.patch("src.live.analysis.AnalysisRuntimeObserver", _FakeRuntimeObserver),
                mock.patch("src.live.analysis.DingTalkNotifier", _FakeDingTalkNotifier),
                mock.patch(
                    "src.live.analysis.resolve_dingtalk_bot_settings",
                    return_value=("https://example.test/robot/send?access_token=x", "secret", ""),
                ),
                mock.patch("src.live.analysis.time.sleep", side_effect=KeyboardInterrupt),
            ):
                code = run_analysis(args)
        self.assertEqual(code, 0)
        self.assertFalse(bool(_FakeRuntimeObserver.init_kwargs.get("enable_data_stall_alert")))
        self.assertEqual(_FakeRuntimeObserver.init_kwargs.get("data_stall_threshold_sec"), 60.0)

    def test_tingwu_enabled_generates_job_and_spawns_worker(self) -> None:
        args = _analysis_args()
        args.tingwu_enabled = True
        payload: dict[str, object] = {}
        cmd: list[str] = []
        with tempfile.TemporaryDirectory() as td:
            args.output_dir = td
            with (
                mock.patch("src.live.analysis.LoginTokenManager", _FakeTokenManager),
                mock.patch("src.live.analysis.fetch_course_meta", return_value=CourseMeta(course_id=1, title="课程", teachers=["老师"])),
                mock.patch("src.live.analysis.JoinRoomClient.try_join", return_value=_JoinResult()),
                mock.patch("src.live.analysis.StreamPoller", _FakePoller),
                mock.patch("src.live.analysis.RealtimeInsightService", _FakeInsightService),
                mock.patch("src.live.analysis.AnalysisRuntimeObserver", _FakeRuntimeObserver),
                mock.patch("src.live.analysis.DingTalkNotifier", _FakeDingTalkNotifier),
                mock.patch("src.live.analysis.AudioOnlyRecorderService", _FakeAudioRecorder),
                mock.patch("src.live.analysis.validate_tingwu_local_requirements", return_value=""),
                mock.patch(
                    "src.live.analysis.resolve_dingtalk_bot_settings",
                    return_value=("https://example.test/robot/send?access_token=x", "secret", ""),
                ),
                mock.patch("src.live.analysis.time.sleep", side_effect=KeyboardInterrupt),
                mock.patch("src.live.analysis.subprocess.Popen") as popen,
            ):
                code = run_analysis(args)
                self.assertEqual(code, 0)
                self.assertTrue(popen.called)
                cmd = popen.call_args.args[0]
                self.assertIn("tingwu-process", cmd)
                self.assertIn("--job-file", cmd)
                job_index = cmd.index("--job-file") + 1
                job_path = Path(cmd[job_index])
                self.assertTrue(job_path.exists())
                payload = json.loads(job_path.read_text(encoding="utf-8"))
        self.assertTrue(str(payload.get("audio_file", "")).endswith("tingwu_audio_full.mp3"))

    def test_tingwu_spawn_failure_sends_status_alert(self) -> None:
        args = _analysis_args()
        args.tingwu_enabled = True
        with tempfile.TemporaryDirectory() as td:
            args.output_dir = td
            with (
                mock.patch("src.live.analysis.LoginTokenManager", _FakeTokenManager),
                mock.patch("src.live.analysis.fetch_course_meta", return_value=CourseMeta(course_id=1, title="课程", teachers=["老师"])),
                mock.patch("src.live.analysis.JoinRoomClient.try_join", return_value=_JoinResult()),
                mock.patch("src.live.analysis.StreamPoller", _FakePoller),
                mock.patch("src.live.analysis.RealtimeInsightService", _FakeInsightService),
                mock.patch("src.live.analysis.AnalysisRuntimeObserver", _FakeRuntimeObserver),
                mock.patch("src.live.analysis.DingTalkNotifier", _FakeDingTalkNotifier),
                mock.patch("src.live.analysis.AudioOnlyRecorderService", _FakeAudioRecorder),
                mock.patch("src.live.analysis.validate_tingwu_local_requirements", return_value=""),
                mock.patch(
                    "src.live.analysis.resolve_dingtalk_bot_settings",
                    return_value=("https://example.test/robot/send?access_token=x", "secret", ""),
                ),
                mock.patch("src.live.analysis.time.sleep", side_effect=KeyboardInterrupt),
                mock.patch("src.live.analysis.subprocess.Popen", side_effect=RuntimeError("boom")),
                mock.patch("src.live.analysis._send_markdown_status") as send_status,
            ):
                code = run_analysis(args)
        self.assertEqual(code, 0)
        self.assertTrue(send_status.called)


if __name__ == "__main__":
    unittest.main()
