from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.live.insight.runtime_monitor import AnalysisRuntimeObserver


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


class _FakeNotifier:
    def __init__(self) -> None:
        self.events = []
        self.stopped = False

    def notify_event(self, event, **kwargs) -> bool:
        self.events.append((event, dict(kwargs)))
        return True

    def stop(self) -> None:
        self.stopped = True


class RuntimeMonitorTests(unittest.TestCase):
    @staticmethod
    def _snapshot(
        *,
        poller_running: bool = True,
        insight_running: bool = True,
        audio_frames_in_total: int = 0,
        asr_final_total: int = 0,
        queue_drop_total: int = 0,
        reconnect_active: bool = False,
        reconnect_elapsed_sec: float = 0.0,
        analysis_ok_total: int = 0,
        analysis_drop_timeout_total: int = 0,
        analysis_drop_error_total: int = 0,
    ) -> dict[str, object]:
        return {
            "poller_running": bool(poller_running),
            "insight_running": bool(insight_running),
            "poller_metrics": {},
            "stream_metrics": {
                "audio_frames_in_total": int(audio_frames_in_total),
                "asr_final_total": int(asr_final_total),
                "queue_drop_total": int(queue_drop_total),
                "reconnect_active": bool(reconnect_active),
                "reconnect_elapsed_sec": float(reconnect_elapsed_sec),
            },
            "stage_metrics": {
                "analysis_ok_total": int(analysis_ok_total),
                "analysis_drop_timeout_total": int(analysis_drop_timeout_total),
                "analysis_drop_error_total": int(analysis_drop_error_total),
            },
        }

    def test_heartbeat_writes_by_interval(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            observer = AnalysisRuntimeObserver(
                session_dir=base,
                notifier=None,
                heartbeat_interval_sec=10.0,
            )
            observer.observe(self._snapshot(), now_mono=100.0)
            observer.observe(self._snapshot(), now_mono=105.0)
            observer.observe(self._snapshot(), now_mono=110.0)

            rows = _read_jsonl(base / "realtime_runtime_heartbeat.jsonl")
            self.assertEqual(len(rows), 2)

    def test_analysis_drop_triggers_p1_and_respects_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            notifier = _FakeNotifier()
            observer = AnalysisRuntimeObserver(
                session_dir=base,
                notifier=notifier,  # type: ignore[arg-type]
                heartbeat_interval_sec=999.0,
                p1_cooldown_sec=45.0,
            )

            observer.observe(self._snapshot(), now_mono=100.0)
            observer.observe(
                self._snapshot(analysis_drop_error_total=1),
                now_mono=101.0,
            )
            observer.observe(
                self._snapshot(analysis_drop_error_total=2),
                now_mono=120.0,
            )
            observer.observe(
                self._snapshot(analysis_drop_error_total=3),
                now_mono=150.0,
            )

            self.assertEqual(len(notifier.events), 2)
            rows = _read_jsonl(base / "realtime_runtime_events.jsonl")
            drop_rows = [row for row in rows if row.get("code") == "analysis_drop_detected"]
            self.assertEqual(len(drop_rows), 3)
            self.assertTrue(bool(drop_rows[0].get("alert_sent")))
            self.assertFalse(bool(drop_rows[1].get("alert_sent")))
            self.assertTrue(bool(drop_rows[2].get("alert_sent")))

    def test_data_stall_and_recovery_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            notifier = _FakeNotifier()
            observer = AnalysisRuntimeObserver(
                session_dir=base,
                notifier=notifier,  # type: ignore[arg-type]
                heartbeat_interval_sec=999.0,
                data_stall_threshold_sec=15.0,
                data_stall_recent_frame_window_sec=5.0,
                p1_cooldown_sec=0.0,
            )

            observer.observe(self._snapshot(), now_mono=0.0)
            observer.observe(self._snapshot(audio_frames_in_total=10), now_mono=1.0)
            observer.observe(self._snapshot(audio_frames_in_total=20), now_mono=17.0)
            observer.observe(
                self._snapshot(audio_frames_in_total=21, asr_final_total=1),
                now_mono=18.0,
            )

            rows = _read_jsonl(base / "realtime_runtime_events.jsonl")
            codes = [str(row.get("code", "")) for row in rows]
            self.assertIn("stream_data_stall", codes)
            self.assertIn("stream_data_stall_recovered", codes)

    def test_reconnect_thresholds_emit_p1_and_p0(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            notifier = _FakeNotifier()
            observer = AnalysisRuntimeObserver(
                session_dir=base,
                notifier=notifier,  # type: ignore[arg-type]
                heartbeat_interval_sec=999.0,
                p0_cooldown_sec=0.0,
                p1_cooldown_sec=0.0,
                reconnect_p1_threshold_sec=20.0,
                reconnect_p0_threshold_sec=60.0,
            )

            observer.observe(self._snapshot(), now_mono=0.0)
            observer.observe(
                self._snapshot(reconnect_active=True, reconnect_elapsed_sec=21.0),
                now_mono=30.0,
            )
            observer.observe(
                self._snapshot(reconnect_active=True, reconnect_elapsed_sec=61.0),
                now_mono=90.0,
            )
            observer.observe(
                self._snapshot(reconnect_active=False, reconnect_elapsed_sec=0.0),
                now_mono=95.0,
            )

            rows = _read_jsonl(base / "realtime_runtime_events.jsonl")
            codes = [str(row.get("code", "")) for row in rows]
            self.assertIn("asr_reconnect_degraded", codes)
            self.assertIn("asr_reconnect_unavailable", codes)
            self.assertIn("asr_reconnect_unavailable_recovered", codes)

    def test_control_plane_down_and_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            notifier = _FakeNotifier()
            observer = AnalysisRuntimeObserver(
                session_dir=base,
                notifier=notifier,  # type: ignore[arg-type]
                heartbeat_interval_sec=999.0,
                p0_cooldown_sec=0.0,
            )

            observer.observe(
                self._snapshot(poller_running=False, insight_running=True),
                now_mono=1.0,
            )
            observer.observe(
                self._snapshot(poller_running=True, insight_running=True),
                now_mono=2.0,
            )

            rows = _read_jsonl(base / "realtime_runtime_events.jsonl")
            codes = [str(row.get("code", "")) for row in rows]
            self.assertIn("control_plane_down", codes)
            self.assertIn("control_plane_recovered", codes)

    def test_watchdog_external_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            notifier = _FakeNotifier()
            observer = AnalysisRuntimeObserver(
                session_dir=base,
                notifier=notifier,  # type: ignore[arg-type]
                heartbeat_interval_sec=999.0,
                p0_cooldown_sec=0.0,
            )
            snapshot = self._snapshot()

            observer.notify_watchdog_restart_failed(
                component="poller",
                error="boom",
                snapshot=snapshot,
                now_mono=10.0,
            )
            observer.notify_watchdog_recovery_pending(
                retry_in_sec=3.0,
                snapshot=snapshot,
                now_mono=11.0,
            )

            rows = _read_jsonl(base / "realtime_runtime_events.jsonl")
            codes = [str(row.get("code", "")) for row in rows]
            self.assertIn("watchdog_restart_failed", codes)
            self.assertIn("watchdog_recovery_pending", codes)
            self.assertEqual(len(notifier.events), 2)


if __name__ == "__main__":
    unittest.main()
