from __future__ import annotations

import argparse
import signal
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from src.live.auto_analysis import (
    _silently_complete_historical_slots_at_startup,
    AnalysisProcessController,
    AutoAnalysisConfig,
    AutoAnalysisInstanceLock,
    AutoAnalysisScheduler,
    AutoRuntimeConfig,
    AutoScanConfig,
    CourseSlotRuntime,
)
from src.scan.live_check import LiveCheckResult


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_markdown(self, *, title: str, text: str) -> tuple[bool, str]:
        self.sent.append((str(title), str(text)))
        return True, ""


class _FakeTokenManager:
    def get_token(self) -> str:
        return "tok"

    def refresh(self, *_args, **_kwargs) -> tuple[bool, str]:
        return True, ""


class _FakeLogQueue:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, msg: str) -> None:
        self.lines.append(str(msg))


def _make_slot(
    *,
    slot_id: str,
    course_id: int,
    start_at: datetime,
    end_at: datetime,
    course_title: str = "课程A",
    teacher: str = "老师A",
) -> CourseSlotRuntime:
    return CourseSlotRuntime(
        slot_id=slot_id,
        course_title=course_title,
        teacher=teacher,
        course_id=course_id,
        start_at=start_at,
        end_at=end_at,
    )


def _build_scheduler(
    *,
    slots: list[CourseSlotRuntime],
    runtime: AutoRuntimeConfig | None = None,
) -> tuple[AutoAnalysisScheduler, _FakeNotifier, _FakeLogQueue]:
    config = AutoAnalysisConfig(
        timezone="Asia/Shanghai",
        scan=AutoScanConfig(),
        runtime=runtime
        or AutoRuntimeConfig(
            no_live_alert_interval_sec=30.0,
            no_live_alert_duration_minutes=15,
            main_tick_sec=1.0,
        ),
        analysis_args={},
        courses=[],
    )
    notifier = _FakeNotifier()
    log_queue = _FakeLogQueue()
    scheduler = AutoAnalysisScheduler(
        args=argparse.Namespace(timeout=20, tenant_code="112", username="", password="", authcode=""),
        config=config,
        token_manager=_FakeTokenManager(),  # type: ignore[arg-type]
        notifier=notifier,  # type: ignore[arg-type]
        slots=slots,
        log_queue=log_queue,  # type: ignore[arg-type]
    )
    return scheduler, notifier, log_queue


def _build_scheduler_and_slot() -> tuple[AutoAnalysisScheduler, CourseSlotRuntime, _FakeNotifier]:
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)
    slot = _make_slot(
        slot_id="slot-1",
        course_id=101,
        start_at=now - timedelta(minutes=1),
        end_at=now + timedelta(minutes=59),
    )
    scheduler, notifier, _log_queue = _build_scheduler(slots=[slot])
    return scheduler, slot, notifier


class AutoAnalysisSchedulerTests(unittest.TestCase):
    def test_live_probe_session_ignores_env_proxy(self) -> None:
        scheduler, _slot, _notifier = _build_scheduler_and_slot()
        self.assertFalse(bool(scheduler._live_session.trust_env))  # noqa: SLF001

    def test_startup_silent_completion_marks_historical_slot_done(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 3, 23, 12, 30, tzinfo=tz)
        slot = _make_slot(
            slot_id="slot-historical",
            course_id=101,
            start_at=now - timedelta(hours=3),
            end_at=now - timedelta(hours=1),
        )
        slot.active_sub_id = "1903390"
        runtime = AutoRuntimeConfig(post_end_guard_minutes=15)
        logs: list[str] = []

        silent_total = _silently_complete_historical_slots_at_startup(
            slots=[slot],
            runtime=runtime,
            now=now,
            log_fn=logs.append,
        )

        self.assertEqual(silent_total, 1)
        self.assertEqual(slot.state, "DONE")
        self.assertTrue(slot.end_notice_sent)
        self.assertEqual(slot.ended_reason, "startup_historical_expired")
        self.assertEqual(slot.active_sub_id, "")
        self.assertFalse(slot.last_probe_is_live)
        self.assertTrue(any("startup silent-completed historical slots=1" in line for line in logs))

    def test_startup_silent_completion_keeps_slot_inside_post_end_guard_active(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 3, 23, 12, 30, tzinfo=tz)
        slot = _make_slot(
            slot_id="slot-guard",
            course_id=101,
            start_at=now - timedelta(hours=2),
            end_at=now - timedelta(minutes=5),
        )
        runtime = AutoRuntimeConfig(post_end_guard_minutes=15)
        logs: list[str] = []

        silent_total = _silently_complete_historical_slots_at_startup(
            slots=[slot],
            runtime=runtime,
            now=now,
            log_fn=logs.append,
        )

        self.assertEqual(silent_total, 0)
        self.assertEqual(slot.state, "PENDING")
        self.assertFalse(slot.end_notice_sent)
        self.assertEqual(slot.ended_reason, "")
        self.assertEqual(logs, [])

    def test_startup_silent_completion_only_marks_historical_slots_in_mixed_schedule(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 3, 23, 12, 30, tzinfo=tz)
        historical_slot = _make_slot(
            slot_id="slot-historical",
            course_id=101,
            start_at=now - timedelta(hours=3),
            end_at=now - timedelta(hours=1),
        )
        future_slot = _make_slot(
            slot_id="slot-future",
            course_id=102,
            start_at=now + timedelta(hours=1),
            end_at=now + timedelta(hours=2),
            course_title="课程B",
            teacher="老师B",
        )
        runtime = AutoRuntimeConfig(post_end_guard_minutes=15)
        logs: list[str] = []

        silent_total = _silently_complete_historical_slots_at_startup(
            slots=[historical_slot, future_slot],
            runtime=runtime,
            now=now,
            log_fn=logs.append,
        )

        unfinished = [slot.slot_id for slot in (historical_slot, future_slot) if slot.state != "DONE"]
        self.assertEqual(silent_total, 1)
        self.assertEqual(historical_slot.state, "DONE")
        self.assertTrue(historical_slot.end_notice_sent)
        self.assertEqual(future_slot.state, "PENDING")
        self.assertFalse(future_slot.end_notice_sent)
        self.assertEqual(unfinished, ["slot-future"])
        self.assertEqual(len(logs), 1)
        self.assertIn("startup silent-completed historical slots=1", logs[0])

    def test_scheduler_exits_cleanly_when_all_slots_are_silently_completed_at_startup(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime(2026, 3, 23, 12, 30, tzinfo=tz)
        slots = [
            _make_slot(
                slot_id="slot-historical-1",
                course_id=101,
                start_at=now - timedelta(hours=4),
                end_at=now - timedelta(hours=2),
            ),
            _make_slot(
                slot_id="slot-historical-2",
                course_id=102,
                start_at=now - timedelta(hours=3),
                end_at=now - timedelta(hours=1),
                course_title="课程B",
                teacher="老师B",
            ),
        ]
        runtime = AutoRuntimeConfig(post_end_guard_minutes=15)
        scheduler, notifier, log_queue = _build_scheduler(slots=slots, runtime=runtime)

        silent_total = _silently_complete_historical_slots_at_startup(
            slots=slots,
            runtime=runtime,
            now=now,
            log_fn=log_queue.log,
        )
        exit_code = scheduler.run()

        self.assertEqual(silent_total, 2)
        self.assertEqual(exit_code, 0)
        self.assertEqual(notifier.sent, [])
        self.assertTrue(any("startup silent-completed historical slots=2" in line for line in log_queue.lines))
        self.assertTrue(any("all slots finished; exiting" in line for line in log_queue.lines))

    def test_done_slot_is_not_probed_again(self) -> None:
        scheduler, slot, _notifier = _build_scheduler_and_slot()
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        slot.state = "DONE"
        slot.end_notice_sent = True
        slot.ended_reason = "live_closed_after_end"
        slot.active_sub_id = "1903390"

        with (
            mock.patch("src.live.auto_analysis.check_course_live_status") as live_check,
            mock.patch.object(scheduler, "_start_analysis") as start_analysis,
        ):
            scheduler._tick_slot(slot, now=now)

        live_check.assert_not_called()
        start_analysis.assert_not_called()

    def test_handle_live_probe_ignores_completed_slot(self) -> None:
        scheduler, slot, _notifier = _build_scheduler_and_slot()
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        slot.state = "DONE"
        slot.end_notice_sent = True
        slot.ended_reason = "live_closed_after_end"

        with mock.patch.object(scheduler, "_start_analysis") as start_analysis:
            scheduler._handle_live_probe_result(
                slot=slot,
                now=now,
                now_mono=10.0,
                result=LiveCheckResult(
                    course_id=slot.course_id,
                    is_live=True,
                    checked=True,
                    attempts=1,
                    elapsed_sec=0.1,
                    last_error="",
                    hint="",
                    sub_id="1903390",
                ),
            )

        start_analysis.assert_not_called()

    def test_probe_failure_alert_replaces_no_live_alert_when_probe_unavailable(self) -> None:
        scheduler, slot, notifier = _build_scheduler_and_slot()
        now = datetime.now(ZoneInfo("Asia/Shanghai"))

        scheduler._handle_live_probe_result(
            slot=slot,
            now=now,
            now_mono=10.0,
            result=LiveCheckResult(
                course_id=slot.course_id,
                is_live=False,
                checked=False,
                attempts=1,
                elapsed_sec=0.1,
                last_error="probe_unavailable",
                hint="dynamic_status_unavailable",
                sub_id="",
            ),
        )

        scheduler._maybe_send_no_live_alert(slot=slot, now=now)
        titles = [title for title, _text in notifier.sent]
        self.assertIn("直播状态探测失败提醒", titles)
        self.assertNotIn("课程未开播提醒", titles)

    def test_probe_failure_alert_is_throttled(self) -> None:
        scheduler, slot, notifier = _build_scheduler_and_slot()
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        result = LiveCheckResult(
            course_id=slot.course_id,
            is_live=False,
            checked=False,
            attempts=1,
            elapsed_sec=0.1,
            last_error="probe_unavailable",
            hint="dynamic_status_unavailable",
            sub_id="",
        )

        scheduler._handle_live_probe_result(slot=slot, now=now, now_mono=10.0, result=result)
        scheduler._handle_live_probe_result(slot=slot, now=now, now_mono=20.0, result=result)
        titles = [title for title, _text in notifier.sent]
        self.assertEqual(titles.count("直播状态探测失败提醒"), 1)

    def test_no_live_alert_requires_checked_non_live_and_not_started(self) -> None:
        scheduler, slot, notifier = _build_scheduler_and_slot()
        now = datetime.now(ZoneInfo("Asia/Shanghai"))

        scheduler._maybe_send_no_live_alert(slot=slot, now=now)
        self.assertEqual(len(notifier.sent), 0)

        slot.last_probe_checked = True
        slot.last_probe_is_live = False
        scheduler._maybe_send_no_live_alert(slot=slot, now=now)
        self.assertEqual(len(notifier.sent), 1)
        self.assertEqual(notifier.sent[0][0], "课程未开播提醒")

        slot.has_started_once = True
        slot.last_no_live_alert_mono = 0.0
        scheduler._maybe_send_no_live_alert(slot=slot, now=now + timedelta(seconds=31))
        self.assertEqual(len(notifier.sent), 1)

        slot.has_started_once = False
        slot.last_probe_is_live = True
        scheduler._maybe_send_no_live_alert(slot=slot, now=now + timedelta(seconds=62))
        self.assertEqual(len(notifier.sent), 1)


class AutoAnalysisInstanceLockTests(unittest.TestCase):
    def test_single_instance_lock_blocks_second_owner(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "auto_analysis.json"
            config_path.write_text("{}", encoding="utf-8")
            lock_a = AutoAnalysisInstanceLock(config_path=config_path)
            lock_b = AutoAnalysisInstanceLock(config_path=config_path)

            ok_a, detail_a = lock_a.acquire()
            self.assertTrue(ok_a)
            self.assertEqual(detail_a, "")

            ok_b, detail_b = lock_b.acquire()
            self.assertFalse(ok_b)
            self.assertIn("lock_file=", detail_b)
            self.assertIn("owner_pid=", detail_b)

            lock_a.release()

            ok_b_retry, detail_b_retry = lock_b.acquire()
            self.assertTrue(ok_b_retry)
            self.assertEqual(detail_b_retry, "")
            lock_b.release()


class AnalysisProcessControllerTests(unittest.TestCase):
    def test_start_uses_new_session(self) -> None:
        controller = AnalysisProcessController(slot_label="slot", log_fn=lambda _msg: None)
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        with mock.patch("src.live.auto_analysis.subprocess.Popen", return_value=fake_proc) as popen:
            ok, err = controller.start(cmd=["python", "-m", "src.main", "analysis"])
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertTrue(popen.called)
        self.assertTrue(bool(popen.call_args.kwargs.get("start_new_session", False)))

    def test_stop_sends_group_signals_in_order(self) -> None:
        controller = AnalysisProcessController(slot_label="slot", log_fn=lambda _msg: None)
        fake_proc = mock.Mock()
        fake_proc.pid = 12345
        fake_proc.poll.return_value = None
        controller._proc = fake_proc  # noqa: SLF001

        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        with (
            mock.patch.object(controller, "_wait_process_exit", side_effect=[False, False, False]),
            mock.patch("src.live.auto_analysis.os.getpgid", return_value=12345),
            mock.patch("src.live.auto_analysis.os.killpg") as killpg,
        ):
            controller.stop(reason="test")

        called_signals = [call.args[1] for call in killpg.call_args_list]
        self.assertEqual(called_signals, [signal.SIGINT, signal.SIGTERM, kill_signal])

    def test_stop_waits_longer_on_course_end_reason(self) -> None:
        controller = AnalysisProcessController(slot_label="slot", log_fn=lambda _msg: None)
        fake_proc = mock.Mock()
        fake_proc.pid = 12345
        fake_proc.poll.return_value = None
        controller._proc = fake_proc  # noqa: SLF001

        wait_calls: list[float] = []

        def _capture_wait(_proc: object, *, timeout_sec: float) -> bool:
            wait_calls.append(float(timeout_sec))
            return False

        with (
            mock.patch.object(controller, "_wait_process_exit", side_effect=_capture_wait),
            mock.patch("src.live.auto_analysis.os.getpgid", return_value=12345),
            mock.patch("src.live.auto_analysis.os.killpg"),
        ):
            controller.stop(reason="live_closed_after_end")

        self.assertEqual(wait_calls, [130.0, 8.0, 2.0])


if __name__ == "__main__":
    unittest.main()
