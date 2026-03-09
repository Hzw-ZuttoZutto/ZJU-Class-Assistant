from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src.common.rotating_log import RotatingLineWriter
from src.live.insight.dingtalk import DingTalkNotifier
from src.live.insight.models import InsightEvent


class AnalysisRuntimeObserver:
    def __init__(
        self,
        *,
        session_dir: Path,
        notifier: DingTalkNotifier | None,
        heartbeat_interval_sec: float = 10.0,
        p0_cooldown_sec: float = 15.0,
        p1_cooldown_sec: float = 45.0,
        data_stall_threshold_sec: float = 15.0,
        data_stall_recent_frame_window_sec: float = 5.0,
        reconnect_p1_threshold_sec: float = 20.0,
        reconnect_p0_threshold_sec: float = 60.0,
        log_rotate_max_bytes: int = 64 * 1024 * 1024,
        log_rotate_backup_count: int = 20,
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self._notifier = notifier
        self._log_fn = log_fn or print
        self._heartbeat_interval_sec = max(0.5, float(heartbeat_interval_sec))
        self._data_stall_threshold_sec = max(1.0, float(data_stall_threshold_sec))
        self._data_stall_recent_frame_window_sec = max(1.0, float(data_stall_recent_frame_window_sec))
        self._reconnect_p1_threshold_sec = max(1.0, float(reconnect_p1_threshold_sec))
        self._reconnect_p0_threshold_sec = max(
            self._reconnect_p1_threshold_sec,
            float(reconnect_p0_threshold_sec),
        )
        self._severity_cooldown_sec = {
            "P0": max(0.0, float(p0_cooldown_sec)),
            "P1": max(0.0, float(p1_cooldown_sec)),
        }

        rotate_max = max(1, int(log_rotate_max_bytes))
        rotate_backup = max(1, int(log_rotate_backup_count))
        self._events_writer = RotatingLineWriter(
            path=session_dir / "realtime_runtime_events.jsonl",
            max_bytes=rotate_max,
            backup_count=rotate_backup,
        )
        self._heartbeat_writer = RotatingLineWriter(
            path=session_dir / "realtime_runtime_heartbeat.jsonl",
            max_bytes=rotate_max,
            backup_count=rotate_backup,
        )

        self._state_lock = threading.Lock()
        self._last_heartbeat_mono = 0.0
        self._last_alert_sent_mono: dict[str, float] = {"P0": 0.0, "P1": 0.0}
        self._runtime_alert_seq = 0

        self._control_plane_degraded = False
        self._reconnect_p1_active = False
        self._reconnect_p0_active = False
        self._data_stall_active = False

        self._analysis_drop_total = 0
        self._queue_drop_total = 0
        self._last_frame_total: int | None = None
        self._last_final_total: int | None = None
        self._last_frame_growth_mono = 0.0
        self._last_final_growth_mono = 0.0

    def observe(self, snapshot: dict[str, object], *, now_mono: float | None = None) -> None:
        now = time.monotonic() if now_mono is None else float(now_mono)
        poller_running = bool(snapshot.get("poller_running", False))
        insight_running = bool(snapshot.get("insight_running", False))
        poller_metrics = _coerce_dict(snapshot.get("poller_metrics"))
        stream_metrics = _coerce_dict(snapshot.get("stream_metrics"))
        stage_metrics = _coerce_dict(snapshot.get("stage_metrics"))

        self._maybe_write_heartbeat(
            now=now,
            poller_running=poller_running,
            insight_running=insight_running,
            poller_metrics=poller_metrics,
            stream_metrics=stream_metrics,
            stage_metrics=stage_metrics,
        )
        self._check_control_plane(
            now=now,
            poller_running=poller_running,
            insight_running=insight_running,
            poller_metrics=poller_metrics,
            stream_metrics=stream_metrics,
            stage_metrics=stage_metrics,
        )
        self._check_analysis_drop(
            now=now,
            poller_running=poller_running,
            insight_running=insight_running,
            poller_metrics=poller_metrics,
            stream_metrics=stream_metrics,
            stage_metrics=stage_metrics,
        )
        self._check_queue_drop(
            now=now,
            poller_running=poller_running,
            insight_running=insight_running,
            poller_metrics=poller_metrics,
            stream_metrics=stream_metrics,
            stage_metrics=stage_metrics,
        )
        self._check_data_stall(
            now=now,
            poller_running=poller_running,
            insight_running=insight_running,
            poller_metrics=poller_metrics,
            stream_metrics=stream_metrics,
            stage_metrics=stage_metrics,
        )
        self._check_reconnect(
            now=now,
            poller_running=poller_running,
            insight_running=insight_running,
            poller_metrics=poller_metrics,
            stream_metrics=stream_metrics,
            stage_metrics=stage_metrics,
        )

    def notify_watchdog_restart_failed(
        self,
        *,
        component: str,
        error: str,
        snapshot: dict[str, object],
        now_mono: float | None = None,
    ) -> None:
        now = time.monotonic() if now_mono is None else float(now_mono)
        message = (
            f"watchdog 重启失败 component={str(component or 'unknown').strip()} "
            f"error={str(error or 'unknown').strip() or 'unknown'}"
        )
        self._emit(
            severity="P0",
            code="watchdog_restart_failed",
            message=message,
            action="立即检查上游连接、ASR链路与认证状态",
            now=now,
            snapshot=snapshot,
        )

    def notify_watchdog_recovery_pending(
        self,
        *,
        retry_in_sec: float,
        snapshot: dict[str, object],
        now_mono: float | None = None,
    ) -> None:
        now = time.monotonic() if now_mono is None else float(now_mono)
        message = f"watchdog 仍在恢复中，预计 {max(0.0, float(retry_in_sec)):.1f}s 后重试"
        self._emit(
            severity="P0",
            code="watchdog_recovery_pending",
            message=message,
            action="如果持续超过 60 秒，请人工介入排障",
            now=now,
            snapshot=snapshot,
        )

    def close(self) -> None:
        notifier = self._notifier
        if notifier is not None:
            notifier.stop()

    def _maybe_write_heartbeat(
        self,
        *,
        now: float,
        poller_running: bool,
        insight_running: bool,
        poller_metrics: dict[str, object],
        stream_metrics: dict[str, object],
        stage_metrics: dict[str, object],
    ) -> None:
        with self._state_lock:
            if self._last_heartbeat_mono > 0 and (now - self._last_heartbeat_mono) < self._heartbeat_interval_sec:
                return
            self._last_heartbeat_mono = now
        payload = {
            "ts_local": datetime.now().astimezone().isoformat(),
            "poller_running": bool(poller_running),
            "insight_running": bool(insight_running),
            "poller_metrics": poller_metrics,
            "stream_metrics": stream_metrics,
            "stage_metrics": stage_metrics,
        }
        self._heartbeat_writer.append(json.dumps(payload, ensure_ascii=False) + "\n")

    def _check_control_plane(
        self,
        *,
        now: float,
        poller_running: bool,
        insight_running: bool,
        poller_metrics: dict[str, object],
        stream_metrics: dict[str, object],
        stage_metrics: dict[str, object],
    ) -> None:
        if not poller_running or not insight_running:
            components: list[str] = []
            if not poller_running:
                components.append("poller")
            if not insight_running:
                components.append("insight")
            self._emit(
                severity="P0",
                code="control_plane_down",
                message=f"关键线程不可用: {', '.join(components)}",
                action="检查线程状态与最近异常日志，确认 watchdog 是否恢复成功",
                now=now,
                snapshot={
                    "poller_running": poller_running,
                    "insight_running": insight_running,
                    "poller_metrics": poller_metrics,
                    "stream_metrics": stream_metrics,
                    "stage_metrics": stage_metrics,
                },
            )
            self._control_plane_degraded = True
            return

        if self._control_plane_degraded:
            self._emit(
                severity="RECOVERY",
                code="control_plane_recovered",
                message="关键线程已恢复运行",
                action="继续观察是否再次出现中断",
                now=now,
                snapshot={
                    "poller_running": poller_running,
                    "insight_running": insight_running,
                    "poller_metrics": poller_metrics,
                    "stream_metrics": stream_metrics,
                    "stage_metrics": stage_metrics,
                },
            )
        self._control_plane_degraded = False

    def _check_analysis_drop(
        self,
        *,
        now: float,
        poller_running: bool,
        insight_running: bool,
        poller_metrics: dict[str, object],
        stream_metrics: dict[str, object],
        stage_metrics: dict[str, object],
    ) -> None:
        drop_timeout = _to_int(stage_metrics.get("analysis_drop_timeout_total"))
        drop_error = _to_int(stage_metrics.get("analysis_drop_error_total"))
        current_total = max(0, drop_timeout + drop_error)
        if current_total <= self._analysis_drop_total:
            return
        delta = current_total - self._analysis_drop_total
        self._analysis_drop_total = current_total
        self._emit(
            severity="P1",
            code="analysis_drop_detected",
            message=(
                f"分析结果丢弃新增 {delta} 条 (timeout={drop_timeout}, error={drop_error}, total={current_total})"
            ),
            action="立即检查模型响应耗时、请求错误率和重试参数",
            now=now,
            snapshot={
                "poller_running": poller_running,
                "insight_running": insight_running,
                "poller_metrics": poller_metrics,
                "stream_metrics": stream_metrics,
                "stage_metrics": stage_metrics,
            },
        )

    def _check_queue_drop(
        self,
        *,
        now: float,
        poller_running: bool,
        insight_running: bool,
        poller_metrics: dict[str, object],
        stream_metrics: dict[str, object],
        stage_metrics: dict[str, object],
    ) -> None:
        queue_drop_total = _to_int(stream_metrics.get("queue_drop_total"))
        if queue_drop_total <= self._queue_drop_total:
            return
        delta = queue_drop_total - self._queue_drop_total
        self._queue_drop_total = queue_drop_total
        self._emit(
            severity="P1",
            code="stream_queue_drop_oldest",
            message=f"stream 分析队列新增丢句 {delta} 条 (total={queue_drop_total})",
            action="提升分析吞吐或下调输入速率，避免进一步丢失",
            now=now,
            snapshot={
                "poller_running": poller_running,
                "insight_running": insight_running,
                "poller_metrics": poller_metrics,
                "stream_metrics": stream_metrics,
                "stage_metrics": stage_metrics,
            },
        )

    def _check_data_stall(
        self,
        *,
        now: float,
        poller_running: bool,
        insight_running: bool,
        poller_metrics: dict[str, object],
        stream_metrics: dict[str, object],
        stage_metrics: dict[str, object],
    ) -> None:
        frame_total = _to_int(stream_metrics.get("audio_frames_in_total"))
        final_total = _to_int(stream_metrics.get("asr_final_total"))

        if self._last_frame_total is None:
            self._last_frame_total = frame_total
            if frame_total > 0:
                self._last_frame_growth_mono = now
        elif frame_total > self._last_frame_total:
            self._last_frame_growth_mono = now
            self._last_frame_total = frame_total
        else:
            self._last_frame_total = frame_total

        if self._last_final_total is None:
            self._last_final_total = final_total
            if final_total > 0:
                self._last_final_growth_mono = now
        elif final_total > self._last_final_total:
            self._last_final_growth_mono = now
            self._last_final_total = final_total
        else:
            self._last_final_total = final_total

        has_recent_frame_input = (
            self._last_frame_growth_mono > 0
            and (now - self._last_frame_growth_mono) <= self._data_stall_recent_frame_window_sec
        )
        no_final_for_too_long = (
            self._last_final_growth_mono <= 0
            or (now - self._last_final_growth_mono) >= self._data_stall_threshold_sec
        )

        if has_recent_frame_input and no_final_for_too_long:
            if not self._data_stall_active:
                self._emit(
                    severity="P1",
                    code="stream_data_stall",
                    message=(
                        f"最近有音频帧输入但 {self._data_stall_threshold_sec:.0f}s 内无 final 句子输出"
                    ),
                    action="检查 ASR 连接状态、输入音频质量与上游流稳定性",
                    now=now,
                    snapshot={
                        "poller_running": poller_running,
                        "insight_running": insight_running,
                        "poller_metrics": poller_metrics,
                        "stream_metrics": stream_metrics,
                        "stage_metrics": stage_metrics,
                    },
                )
            self._data_stall_active = True
            return

        if self._data_stall_active and not no_final_for_too_long:
            self._emit(
                severity="RECOVERY",
                code="stream_data_stall_recovered",
                message="数据面卡顿已恢复，final 句子输出重新出现",
                action="继续观察吞吐与延迟趋势",
                now=now,
                snapshot={
                    "poller_running": poller_running,
                    "insight_running": insight_running,
                    "poller_metrics": poller_metrics,
                    "stream_metrics": stream_metrics,
                    "stage_metrics": stage_metrics,
                },
            )
        self._data_stall_active = False

    def _check_reconnect(
        self,
        *,
        now: float,
        poller_running: bool,
        insight_running: bool,
        poller_metrics: dict[str, object],
        stream_metrics: dict[str, object],
        stage_metrics: dict[str, object],
    ) -> None:
        reconnect_active = bool(stream_metrics.get("reconnect_active", False))
        reconnect_elapsed_sec = _to_float(stream_metrics.get("reconnect_elapsed_sec"))

        if reconnect_active and reconnect_elapsed_sec >= self._reconnect_p1_threshold_sec:
            if not self._reconnect_p1_active:
                self._emit(
                    severity="P1",
                    code="asr_reconnect_degraded",
                    message=f"ASR 重连已持续 {reconnect_elapsed_sec:.1f}s (>= {self._reconnect_p1_threshold_sec:.1f}s)",
                    action="检查 DashScope 连接质量与网络抖动",
                    now=now,
                    snapshot={
                        "poller_running": poller_running,
                        "insight_running": insight_running,
                        "poller_metrics": poller_metrics,
                        "stream_metrics": stream_metrics,
                        "stage_metrics": stage_metrics,
                    },
                )
            self._reconnect_p1_active = True
        elif self._reconnect_p1_active:
            self._emit(
                severity="RECOVERY",
                code="asr_reconnect_degraded_recovered",
                message="ASR 重连退化状态已恢复",
                action="继续观察重连频率和时长",
                now=now,
                snapshot={
                    "poller_running": poller_running,
                    "insight_running": insight_running,
                    "poller_metrics": poller_metrics,
                    "stream_metrics": stream_metrics,
                    "stage_metrics": stage_metrics,
                },
            )
            self._reconnect_p1_active = False

        if reconnect_active and reconnect_elapsed_sec >= self._reconnect_p0_threshold_sec:
            if not self._reconnect_p0_active:
                self._emit(
                    severity="P0",
                    code="asr_reconnect_unavailable",
                    message=(
                        f"ASR 重连长时间未恢复，已持续 {reconnect_elapsed_sec:.1f}s "
                        f"(>= {self._reconnect_p0_threshold_sec:.1f}s)"
                    ),
                    action="立即人工排查 ASR 服务可用性与网络连通性",
                    now=now,
                    snapshot={
                        "poller_running": poller_running,
                        "insight_running": insight_running,
                        "poller_metrics": poller_metrics,
                        "stream_metrics": stream_metrics,
                        "stage_metrics": stage_metrics,
                    },
                )
            self._reconnect_p0_active = True
        elif self._reconnect_p0_active:
            self._emit(
                severity="RECOVERY",
                code="asr_reconnect_unavailable_recovered",
                message="ASR 长时不可用状态已恢复",
                action="继续观察服务稳定性",
                now=now,
                snapshot={
                    "poller_running": poller_running,
                    "insight_running": insight_running,
                    "poller_metrics": poller_metrics,
                    "stream_metrics": stream_metrics,
                    "stage_metrics": stage_metrics,
                },
            )
            self._reconnect_p0_active = False

    def _emit(
        self,
        *,
        severity: str,
        code: str,
        message: str,
        action: str,
        now: float,
        snapshot: dict[str, object],
    ) -> None:
        normalized_severity = str(severity or "").strip().upper() or "INFO"
        normalized_code = str(code or "").strip().lower() or "runtime_event"
        normalized_message = str(message or "").strip() or normalized_code
        normalized_action = str(action or "").strip()

        alert_sent = False
        alert_skip_reason = ""
        if normalized_severity in {"P0", "P1"}:
            alert_sent, alert_skip_reason = self._send_runtime_alert(
                severity=normalized_severity,
                code=normalized_code,
                message=normalized_message,
                action=normalized_action,
                now=now,
                snapshot=snapshot,
            )

        payload = {
            "ts_local": datetime.now().astimezone().isoformat(),
            "severity": normalized_severity,
            "code": normalized_code,
            "message": normalized_message,
            "action": normalized_action,
            "alert_sent": bool(alert_sent),
            "alert_skip_reason": alert_skip_reason,
            "snapshot": _coerce_dict(snapshot),
        }
        self._events_writer.append(json.dumps(payload, ensure_ascii=False) + "\n")

    def _send_runtime_alert(
        self,
        *,
        severity: str,
        code: str,
        message: str,
        action: str,
        now: float,
        snapshot: dict[str, object],
    ) -> tuple[bool, str]:
        notifier = self._notifier
        if notifier is None:
            return False, "runtime_notifier_unavailable"

        cooldown = self._severity_cooldown_sec.get(severity, 0.0)
        with self._state_lock:
            last = self._last_alert_sent_mono.get(severity, 0.0)
            if cooldown > 0 and last > 0 and (now - last) < cooldown:
                remain = cooldown - (now - last)
                return False, f"severity_cooldown_{severity}_{remain:.1f}s"
            self._runtime_alert_seq += 1
            alert_seq = int(self._runtime_alert_seq)

        title = f"[{severity}] {message}"
        if len(title) > 80:
            title = title[:77] + "..."
        details = [
            f"code={code}",
            f"poller_running={bool(snapshot.get('poller_running', False))}",
            f"insight_running={bool(snapshot.get('insight_running', False))}",
        ]
        event = InsightEvent(
            ts=datetime.now().astimezone(),
            chunk_seq=alert_seq,
            chunk_file=f"runtime_event_{alert_seq:06d}.json",
            model="runtime_monitor",
            important=True,
            summary=f"[{severity}] {message}",
            context_summary="analysis 运行态异常",
            event_type="runtime_alert",
            headline=title,
            immediate_action=action or "请立即检查运行状态",
            key_details=details,
            matched_terms=[],
            reason=code,
            attempt_count=1,
            context_chunk_count=0,
            status="runtime_alert",
            error="",
        )
        try:
            accepted = bool(notifier.notify_event(event))
        except Exception as exc:  # pragma: no cover - defensive
            self._log_fn(f"[analysis][runtime-alert] send failed severity={severity} code={code} error={exc}")
            return False, f"notify_exception:{exc}"

        if accepted:
            with self._state_lock:
                self._last_alert_sent_mono[severity] = now
            return True, ""
        return False, "notifier_rejected"


def _coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    return {}


def _to_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _to_float(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0
