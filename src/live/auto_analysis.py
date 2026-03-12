from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from src.auth import LoginTokenManager
from src.auth.cas_client import ZJUAuthClient
from src.cli.parser import build_parser
from src.common.account import resolve_credentials, resolve_dingtalk_bot_settings
from src.common.billing import (
    BILLING_ALERT_COOLDOWN_SEC,
    consume_billing_alert_cooldown,
    detect_billing_issue,
)
from src.common.course_meta import course_teachers, query_course_detail
from src.common.http import create_session, get_thread_session
from src.common.rotating_log import RotatingLineWriter
from src.live.analysis import _validate_analysis_args
from src.live.tingwu import run_tingwu_remote_preflight, validate_tingwu_local_requirements
from src.scan.live_check import LiveCheckResult, check_course_live_status

_SH_TZ = ZoneInfo("Asia/Shanghai")
_DEFAULT_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class AutoCourseSlot:
    start: datetime
    end: datetime


@dataclass
class AutoCourseSpec:
    course_id: int
    title: str
    teacher: str
    slots: list[AutoCourseSlot] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, str, str]:
        return self.course_id, self.title, self.teacher


@dataclass
class AutoScanConfig:
    center: int = 82000
    radius: int = 10000
    workers: int = 64
    retries: int = 1
    show_progress: bool = True
    stop_when_all_found: bool = True


@dataclass
class AutoRuntimeConfig:
    pre_start_notice_minutes: int = 15
    near_start_probe_interval_sec: float = 2.0
    after_start_probe_interval_sec: float = 2.0
    late_probe_interval_sec: float = 30.0
    near_end_probe_interval_sec: float = 2.0
    post_end_guard_minutes: int = 15
    no_live_alert_interval_sec: float = 30.0
    no_live_alert_duration_minutes: int = 15
    retry_alert_min_interval_sec: float = 30.0
    main_tick_sec: float = 1.0


@dataclass
class AutoAnalysisConfig:
    timezone: str
    scan: AutoScanConfig
    runtime: AutoRuntimeConfig
    analysis_args: dict[str, Any]
    courses: list[AutoCourseSpec]


@dataclass
class CourseSlotRuntime:
    slot_id: str
    course_title: str
    teacher: str
    course_id: int
    start_at: datetime
    end_at: datetime
    state: str = "PENDING"
    pre_notice_sent: bool = False
    start_notice_sent: bool = False
    end_notice_sent: bool = False
    active_sub_id: str = ""
    has_started_once: bool = False
    last_probe_mono: float = 0.0
    last_probe_checked: bool = False
    last_probe_is_live: bool = False
    last_probe_at: datetime | None = None
    last_no_live_alert_mono: float = 0.0
    last_probe_failure_alert_mono: float = 0.0
    last_retry_alert_mono: float = 0.0
    last_subid_missing_alert_mono: float = 0.0
    start_attempt_total: int = 0
    restart_total: int = 0
    last_probe_error: str = ""
    ended_reason: str = ""

    def label(self) -> str:
        return (
            f"{self.course_title} | {self.teacher} | "
            f"{self.start_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )


class AutoLogQueue:
    def __init__(
        self,
        *,
        path: Path,
        queue_size: int = 5000,
        rotate_max_bytes: int = 64 * 1024 * 1024,
        rotate_backup_count: int = 20,
    ) -> None:
        self._writer = RotatingLineWriter(
            path=path,
            max_bytes=max(1, int(rotate_max_bytes)),
            backup_count=max(1, int(rotate_backup_count)),
        )
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._run, name="auto-analysis-log", daemon=True)
        self._drop_total = 0
        self._last_drop_log_at = 0.0
        self._worker.start()

    def log(self, msg: str) -> None:
        ts = datetime.now(_SH_TZ).strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}\n"
        print(line.rstrip())
        self._enqueue(line)

    def close(self) -> None:
        self._stop_event.set()
        self._enqueue_sentinel()
        self._worker.join(timeout=3.0)

    def _enqueue(self, line: str) -> None:
        try:
            self._queue.put_nowait(line)
            return
        except queue.Full:
            pass

        try:
            _ = self._queue.get_nowait()
            self._queue.task_done()
            self._drop_total += 1
        except queue.Empty:
            pass

        try:
            self._queue.put_nowait(line)
        except queue.Full:
            self._drop_total += 1
            return

        now = time.monotonic()
        if self._drop_total <= 1 or (now - self._last_drop_log_at) >= 10.0:
            self._last_drop_log_at = now
            warning = f"[auto-analysis] log queue overflow dropped_total={self._drop_total}\n"
            try:
                self._queue.put_nowait(warning)
            except queue.Full:
                pass

    def _enqueue_sentinel(self) -> None:
        while True:
            try:
                self._queue.put_nowait(None)
                return
            except queue.Full:
                try:
                    _ = self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    return

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._writer.append(item)
            finally:
                self._queue.task_done()


class AutoAnalysisInstanceLock:
    def __init__(self, *, config_path: Path) -> None:
        self._config_path = config_path
        self._lock_path = config_path.with_name(f"{config_path.name}.lock")
        self._fh = None

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def acquire(self) -> tuple[bool, str]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = self._lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            owner = self._read_owner_payload()
            try:
                fh.close()
            except Exception:
                pass
            return False, self._format_owner(owner)
        except OSError as exc:
            try:
                fh.close()
            except Exception:
                pass
            return False, f"lock acquire failed: {exc}"

        payload = {
            "pid": os.getpid(),
            "config_path": str(self._config_path),
            "started_at": datetime.now(_SH_TZ).isoformat(),
        }
        try:
            fh.seek(0)
            fh.truncate(0)
            fh.write(json.dumps(payload, ensure_ascii=False))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        except Exception:
            pass
        self._fh = fh
        return True, ""

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            fh.seek(0)
            fh.truncate(0)
            fh.flush()
            os.fsync(fh.fileno())
        except Exception:
            pass
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            fh.close()
        except Exception:
            pass
        self._fh = None

    def _read_owner_payload(self) -> dict[str, str]:
        try:
            raw = self._lock_path.read_text(encoding="utf-8").strip()
            if not raw:
                return {}
            payload = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(k): str(v) for k, v in payload.items()}

    def _format_owner(self, payload: dict[str, str]) -> str:
        pid = str(payload.get("pid", "")).strip()
        started_at = str(payload.get("started_at", "")).strip()
        config_path = str(payload.get("config_path", "")).strip()
        parts = [f"lock_file={self._lock_path}"]
        if pid:
            parts.append(f"owner_pid={pid}")
        if started_at:
            parts.append(f"owner_started_at={started_at}")
        if config_path:
            parts.append(f"owner_config={config_path}")
        return ", ".join(parts)


class DingTalkMarkdownSender:
    def __init__(
        self,
        *,
        webhook: str,
        secret: str,
        timeout_sec: float = 5.0,
        retry_count: int = 3,
    ) -> None:
        self.webhook = str(webhook or "").strip()
        self.secret = str(secret or "").strip()
        self.timeout_sec = max(1.0, float(timeout_sec))
        self.retry_count = max(1, int(retry_count))
        if not self.webhook:
            raise ValueError("DingTalk webhook is empty")
        if not self.secret:
            raise ValueError("DingTalk secret is empty")

    def send_markdown(self, *, title: str, text: str) -> tuple[bool, str]:
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": str(title or "").strip() or "课程提醒",
                "text": str(text or "").strip() or "课程提醒",
            },
        }
        last_error = ""
        for attempt in range(1, self.retry_count + 1):
            try:
                timestamp_ms = int(time.time() * 1000)
                url = self._build_signed_webhook_url(timestamp_ms=timestamp_ms)
                resp = get_thread_session(pool_size=8).post(
                    url,
                    json=payload,
                    timeout=self.timeout_sec,
                )
                resp.raise_for_status()
                body = resp.json()
                if int(body.get("errcode", -1)) != 0:
                    raise RuntimeError(
                        f"errcode={body.get('errcode')} errmsg={body.get('errmsg', '')}"
                    )
                return True, ""
            except Exception as exc:
                last_error = str(exc)
                if attempt >= self.retry_count:
                    break
                time.sleep(min(4.0, 0.5 * attempt))
        return False, last_error

    def _build_signed_webhook_url(self, *, timestamp_ms: int) -> str:
        to_sign = f"{int(timestamp_ms)}\n{self.secret}"
        digest = hmac.new(
            self.secret.encode("utf-8"),
            to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = quote_plus(base64.b64encode(digest).decode("utf-8"))
        separator = "&" if "?" in self.webhook else "?"
        return f"{self.webhook}{separator}timestamp={int(timestamp_ms)}&sign={sign}"


class AnalysisProcessController:
    def __init__(
        self,
        *,
        slot_label: str,
        log_fn,
    ) -> None:
        self.slot_label = slot_label
        self._log_fn = log_fn
        self._proc: subprocess.Popen | None = None
        self._expected_stop = False
        self._last_exit_code: int | None = None

    def is_running(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        return proc.poll() is None

    @property
    def last_exit_code(self) -> int | None:
        return self._last_exit_code

    def reap(self) -> tuple[bool, int | None, bool]:
        proc = self._proc
        if proc is None:
            return False, self._last_exit_code, self._expected_stop
        code = proc.poll()
        if code is None:
            return False, self._last_exit_code, self._expected_stop
        expected = self._expected_stop
        self._last_exit_code = int(code)
        self._proc = None
        self._expected_stop = False
        return True, self._last_exit_code, expected

    def start(self, *, cmd: list[str]) -> tuple[bool, str]:
        if self.is_running():
            return True, ""
        self._expected_stop = False
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True, ""
        except Exception as exc:
            self._proc = None
            self._last_exit_code = None
            return False, str(exc)

    def stop(self, *, reason: str) -> None:
        proc = self._proc
        if proc is None:
            return
        self._expected_stop = True
        self._log_fn(f"[slot] stopping analysis ({self.slot_label}) reason={reason}")

        if proc.poll() is not None:
            self._last_exit_code = proc.returncode
            self._proc = None
            self._expected_stop = False
            return

        graceful_reasons = {
            "live_closed_after_end",
            "post_end_guard_timeout",
            "scheduler_shutdown",
        }
        sigint_wait_sec = 130.0 if reason in graceful_reasons else 5.0
        sigterm_wait_sec = 8.0 if reason in graceful_reasons else 5.0

        self._send_process_group_signal(proc, signal.SIGINT)
        if self._wait_process_exit(proc, timeout_sec=sigint_wait_sec):
            return

        self._log_fn(
            f"[slot] analysis still running after SIGINT label={self.slot_label} "
            f"waited={sigint_wait_sec:.1f}s reason={reason}, escalating to SIGTERM"
        )
        self._send_process_group_signal(proc, signal.SIGTERM)
        if self._wait_process_exit(proc, timeout_sec=sigterm_wait_sec):
            return

        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        self._log_fn(
            f"[slot] analysis still running after SIGTERM label={self.slot_label} "
            f"waited={sigterm_wait_sec:.1f}s reason={reason}, escalating to {kill_signal.name}"
        )
        self._send_process_group_signal(proc, kill_signal)
        _ = self._wait_process_exit(proc, timeout_sec=2.0)

    def _wait_process_exit(self, proc: subprocess.Popen, *, timeout_sec: float) -> bool:
        deadline = time.monotonic() + max(0.1, float(timeout_sec))
        while time.monotonic() < deadline:
            code = proc.poll()
            if code is not None:
                self._last_exit_code = int(code)
                self._proc = None
                self._expected_stop = False
                return True
            time.sleep(0.1)
        return False

    def _send_process_group_signal(self, proc: subprocess.Popen, signum: int) -> None:
        if proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return
        except Exception as exc:
            self._log_fn(
                f"[slot] getpgid failed label={self.slot_label} pid={getattr(proc, 'pid', '?')} error={exc}"
            )
            try:
                proc.send_signal(signum)
            except Exception:
                pass
            return

        try:
            os.killpg(pgid, signum)
        except ProcessLookupError:
            return
        except Exception as exc:
            self._log_fn(
                f"[slot] killpg failed label={self.slot_label} pgid={pgid} signum={int(signum)} error={exc}"
            )
            try:
                proc.send_signal(signum)
            except Exception:
                pass


class AutoAnalysisScheduler:
    def __init__(
        self,
        *,
        args: argparse.Namespace,
        config: AutoAnalysisConfig,
        token_manager: LoginTokenManager,
        notifier: DingTalkMarkdownSender,
        slots: list[CourseSlotRuntime],
        log_queue: AutoLogQueue,
        stop_event: threading.Event | None = None,
        stop_reason_ref: dict[str, str] | None = None,
    ) -> None:
        self.args = args
        self.config = config
        self.token_manager = token_manager
        self.notifier = notifier
        self.slots = slots
        self.log = log_queue.log
        self._tz = ZoneInfo(config.timezone)
        self._runtime = config.runtime
        self._analysis_args = dict(config.analysis_args)
        self._live_session = create_session(pool_size=16)
        # Avoid system proxy/WAF intermediate pages that break live-status JSON.
        self._live_session.trust_env = False
        self._token_refresh_last_mono = 0.0
        self._stop_event = stop_event
        self._stop_reason_ref = stop_reason_ref or {}
        self._controllers: dict[str, AnalysisProcessController] = {
            slot.slot_id: AnalysisProcessController(slot_label=slot.label(), log_fn=self.log)
            for slot in slots
        }

    def run(self) -> int:
        self.log(f"[auto-analysis] schedule loaded slots={len(self.slots)}")
        try:
            while True:
                if self._is_stop_requested():
                    reason = self._stop_reason()
                    self.log(f"[auto-analysis] stop requested ({reason}), stopping all running analyses")
                    return 130
                now = datetime.now(self._tz)
                self._maybe_refresh_token()

                unfinished = 0
                for slot in self.slots:
                    if slot.state != "DONE":
                        unfinished += 1
                    self._tick_slot(slot, now=now)

                if unfinished <= 0:
                    self.log("[auto-analysis] all slots finished; exiting")
                    return 0
                if self._wait_next_tick():
                    reason = self._stop_reason()
                    self.log(f"[auto-analysis] stop requested ({reason}), stopping all running analyses")
                    return 130
        except KeyboardInterrupt:
            self.log("[auto-analysis] interrupted by user, stopping all running analyses")
            return 130
        finally:
            self._shutdown_all_processes()
            try:
                self._live_session.close()
            except Exception:
                pass

    def _is_stop_requested(self) -> bool:
        event = self._stop_event
        if event is None:
            return False
        return bool(event.is_set())

    def _stop_reason(self) -> str:
        reason = str(self._stop_reason_ref.get("reason", "") or "").strip()
        return reason or "signal"

    def _wait_next_tick(self) -> bool:
        wait_sec = max(0.2, float(self._runtime.main_tick_sec))
        event = self._stop_event
        if event is None:
            time.sleep(wait_sec)
            return False
        return bool(event.wait(timeout=wait_sec))

    def _tick_slot(self, slot: CourseSlotRuntime, *, now: datetime) -> None:
        controller = self._controllers[slot.slot_id]
        exited, exit_code, expected_stop = controller.reap()
        if exited:
            self.log(
                f"[slot] analysis exited label={slot.label()} code={exit_code} expected={expected_stop}"
            )
            if (not expected_stop) and now <= self._slot_guard_end(slot):
                self._notify_retry_throttled(
                    slot=slot,
                    reason=f"analysis exited unexpectedly (code={exit_code})",
                    now_mono=time.monotonic(),
                )

        guard_end = self._slot_guard_end(slot)
        preheat_start = slot.start_at - timedelta(minutes=max(0, self._runtime.pre_start_notice_minutes))

        if now >= guard_end:
            if controller.is_running():
                controller.stop(reason="post_end_guard_timeout")
            if not slot.end_notice_sent:
                slot.end_notice_sent = True
                slot.ended_reason = slot.ended_reason or "post_end_guard_timeout"
                self._notify_course_end(slot=slot, now=now, reason=slot.ended_reason)
            slot.state = "DONE"
            return

        if (not slot.pre_notice_sent) and now >= preheat_start:
            slot.pre_notice_sent = True
            self._notify_pre_start(slot=slot, now=now)

        if controller.is_running():
            slot.state = "RUNNING"
        else:
            slot.state = "WAIT_LIVE" if now >= slot.start_at else ("PREHEAT" if now >= preheat_start else "PENDING")

        probe_interval = self._probe_interval(slot=slot, now=now)
        if probe_interval is not None:
            self._maybe_probe(slot=slot, now=now, probe_interval=probe_interval)

        if (not controller.is_running()) and now >= slot.start_at:
            self._maybe_send_no_live_alert(slot=slot, now=now)

        if controller.is_running() and now >= slot.start_at and now <= guard_end:
            slot.state = "RUNNING"

    def _probe_interval(self, *, slot: CourseSlotRuntime, now: datetime) -> float | None:
        if now < slot.start_at - timedelta(minutes=max(0, self._runtime.pre_start_notice_minutes)):
            return None
        if now < slot.start_at:
            return max(0.2, float(self._runtime.near_start_probe_interval_sec))
        if now < slot.end_at:
            alert_end = slot.start_at + timedelta(minutes=max(0, self._runtime.no_live_alert_duration_minutes))
            if now <= alert_end:
                return max(0.2, float(self._runtime.after_start_probe_interval_sec))
            return max(0.2, float(self._runtime.late_probe_interval_sec))
        return max(0.2, float(self._runtime.near_end_probe_interval_sec))

    def _maybe_probe(self, *, slot: CourseSlotRuntime, now: datetime, probe_interval: float) -> None:
        now_mono = time.monotonic()
        if slot.last_probe_mono > 0 and (now_mono - slot.last_probe_mono) < probe_interval:
            return
        slot.last_probe_mono = now_mono

        token = self.token_manager.get_token()
        result = check_course_live_status(
            session=self._live_session,
            token=token,
            timeout=int(self.args.timeout),
            tenant_code=str(self.args.tenant_code),
            course_id=int(slot.course_id),
            max_wait_sec=0.0,
            interval_sec=0.0,
        )
        self._handle_live_probe_result(slot=slot, now=now, now_mono=now_mono, result=result)

    def _handle_live_probe_result(
        self,
        *,
        slot: CourseSlotRuntime,
        now: datetime,
        now_mono: float,
        result: LiveCheckResult,
    ) -> None:
        controller = self._controllers[slot.slot_id]
        slot.last_probe_at = now
        slot.last_probe_checked = bool(result.checked)
        slot.last_probe_is_live = bool(result.is_live and result.checked)
        if not result.checked:
            slot.last_probe_is_live = False
            slot.last_probe_error = str(result.last_error or "probe_unavailable")
            self.log(
                f"[slot] probe unavailable label={slot.label()} error={slot.last_probe_error} hint={result.hint}"
            )
            self._maybe_send_probe_failure_alert(
                slot=slot,
                now=now,
                now_mono=now_mono,
                result=result,
            )
            return

        slot.last_probe_error = ""
        if result.is_live:
            sub_id = str(result.sub_id or "").strip()
            if not sub_id:
                if (
                    slot.last_subid_missing_alert_mono <= 0
                    or (now_mono - slot.last_subid_missing_alert_mono) >= max(1.0, self._runtime.retry_alert_min_interval_sec)
                ):
                    slot.last_subid_missing_alert_mono = now_mono
                    self._notify_live_without_subid(slot=slot, now=now)
                return

            if not controller.is_running():
                self._start_analysis(slot=slot, sub_id=sub_id, now=now, reason="live_detected")
                return

            if slot.active_sub_id != sub_id:
                self.log(
                    f"[slot] sub_id drift label={slot.label()} old={slot.active_sub_id} new={sub_id}, restarting"
                )
                slot.restart_total += 1
                controller.stop(reason="sub_id_drift")
                self._start_analysis(slot=slot, sub_id=sub_id, now=now, reason="sub_id_drift")
            return

        if now >= slot.end_at and controller.is_running():
            controller.stop(reason="live_closed_after_end")
            if not slot.end_notice_sent:
                slot.end_notice_sent = True
                slot.ended_reason = "live_closed_after_end"
                self._notify_course_end(slot=slot, now=now, reason=slot.ended_reason)
            slot.state = "DONE"
            return

        if (not controller.is_running()) and slot.active_sub_id and now <= self._slot_guard_end(slot):
            if now >= slot.end_at:
                if not slot.end_notice_sent:
                    slot.end_notice_sent = True
                    slot.ended_reason = "live_closed_after_end"
                    self._notify_course_end(slot=slot, now=now, reason=slot.ended_reason)
                slot.state = "DONE"
                return
            self._start_analysis(slot=slot, sub_id=slot.active_sub_id, now=now, reason="retry_after_exit")

    def _start_analysis(
        self,
        *,
        slot: CourseSlotRuntime,
        sub_id: str,
        now: datetime,
        reason: str,
    ) -> None:
        controller = self._controllers[slot.slot_id]
        cmd = self._build_analysis_command(course_id=slot.course_id, sub_id=sub_id)
        ok, error = controller.start(cmd=cmd)
        if not ok:
            self.log(f"[slot] failed to start analysis label={slot.label()} error={error}")
            self._notify_retry_throttled(
                slot=slot,
                reason=f"failed to start analysis: {error}",
                now_mono=time.monotonic(),
            )
            return

        slot.active_sub_id = sub_id
        slot.has_started_once = True
        slot.start_attempt_total += 1
        slot.state = "RUNNING"
        if not slot.start_notice_sent:
            slot.start_notice_sent = True
            self._notify_course_start(slot=slot, now=now, sub_id=sub_id)
        else:
            self._notify_retry_throttled(
                slot=slot,
                reason=f"analysis restarted ({reason}) sub_id={sub_id}",
                now_mono=time.monotonic(),
                force=True,
            )
        self.log(
            f"[slot] analysis started label={slot.label()} sub_id={sub_id} reason={reason} cmd={' '.join(cmd)}"
        )

    def _maybe_send_no_live_alert(self, *, slot: CourseSlotRuntime, now: datetime) -> None:
        if slot.has_started_once:
            return
        if not slot.last_probe_checked:
            return
        if slot.last_probe_is_live:
            return
        alert_until = slot.start_at + timedelta(minutes=max(0, self._runtime.no_live_alert_duration_minutes))
        if now < slot.start_at or now > alert_until:
            return
        now_mono = time.monotonic()
        if (
            slot.last_no_live_alert_mono > 0
            and (now_mono - slot.last_no_live_alert_mono) < max(1.0, self._runtime.no_live_alert_interval_sec)
        ):
            return
        slot.last_no_live_alert_mono = now_mono
        delay_sec = max(0.0, (now - slot.start_at).total_seconds())
        title = "课程未开播提醒"
        text = "\n".join(
            [
                f"# 课程未开播提醒",
                "",
                f"- 课程：{slot.course_title} | {slot.teacher}",
                f"- 计划开始：{slot.start_at.strftime('%Y-%m-%d %H:%M:%S')}",
                f"- 当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
                f"- 已延迟：{int(delay_sec)} 秒",
            ]
        )
        self._send_dingtalk(title=title, text=text, slot=slot)

    def _maybe_send_probe_failure_alert(
        self,
        *,
        slot: CourseSlotRuntime,
        now: datetime,
        now_mono: float,
        result: LiveCheckResult,
    ) -> None:
        controller = self._controllers[slot.slot_id]
        if controller.is_running():
            return
        alert_until = slot.start_at + timedelta(minutes=max(0, self._runtime.no_live_alert_duration_minutes))
        if now < slot.start_at or now > alert_until:
            return
        if (
            slot.last_probe_failure_alert_mono > 0
            and (now_mono - slot.last_probe_failure_alert_mono) < max(1.0, self._runtime.no_live_alert_interval_sec)
        ):
            return
        slot.last_probe_failure_alert_mono = now_mono
        error_text = str(result.last_error or "dynamic_status_unavailable").strip() or "dynamic_status_unavailable"
        if len(error_text) > 300:
            error_text = error_text[:297] + "..."
        hint = str(result.hint or "dynamic_status_unavailable").strip() or "dynamic_status_unavailable"
        title = "直播状态探测失败提醒"
        text = "\n".join(
            [
                "# 直播状态探测失败提醒",
                "",
                f"- 课程：{slot.course_title} | {slot.teacher}",
                f"- 计划开始：{slot.start_at.strftime('%Y-%m-%d %H:%M:%S')}",
                f"- 当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
                f"- hint={hint}",
                f"- error={error_text}",
            ]
        )
        self._send_dingtalk(title=title, text=text, slot=slot)

    def _notify_pre_start(self, *, slot: CourseSlotRuntime, now: datetime) -> None:
        title = "课程即将开始"
        text = "\n".join(
            [
                "# 课程即将开始",
                "",
                f"- 课程：{slot.course_title} | {slot.teacher}",
                f"- 开始时间：{slot.start_at.strftime('%Y-%m-%d %H:%M:%S')}",
                f"- 提醒时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
            ]
        )
        self._send_dingtalk(title=title, text=text, slot=slot)

    def _notify_course_start(self, *, slot: CourseSlotRuntime, now: datetime, sub_id: str) -> None:
        title = "课程开始"
        text = "\n".join(
            [
                "# 课程开始",
                "",
                f"- 课程：{slot.course_title} | {slot.teacher}",
                f"- 开始时间：{slot.start_at.strftime('%Y-%m-%d %H:%M:%S')}",
                f"- 检测时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
                f"- course_id={slot.course_id}, sub_id={sub_id}",
            ]
        )
        self._send_dingtalk(title=title, text=text, slot=slot)

    def _notify_course_end(self, *, slot: CourseSlotRuntime, now: datetime, reason: str) -> None:
        title = "课程结束"
        text = "\n".join(
            [
                "# 课程结束",
                "",
                f"- 课程：{slot.course_title} | {slot.teacher}",
                f"- 计划结束：{slot.end_at.strftime('%Y-%m-%d %H:%M:%S')}",
                f"- 实际结束处理时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
                f"- reason={reason}",
            ]
        )
        self._send_dingtalk(title=title, text=text, slot=slot)

    def _notify_live_without_subid(self, *, slot: CourseSlotRuntime, now: datetime) -> None:
        title = "直播异常：缺少 sub_id"
        text = "\n".join(
            [
                "# 直播异常：缺少 sub_id",
                "",
                f"- 课程：{slot.course_title} | {slot.teacher}",
                f"- 时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
                f"- course_id={slot.course_id}",
                "- 状态：检测到直播中，但接口未返回 sub_id，已阻止自动启动/切换 analysis。",
            ]
        )
        self._send_dingtalk(title=title, text=text, slot=slot)

    def _notify_retry_throttled(
        self,
        *,
        slot: CourseSlotRuntime,
        reason: str,
        now_mono: float,
        force: bool = False,
    ) -> None:
        interval = max(0.0, float(self._runtime.retry_alert_min_interval_sec))
        if (not force) and slot.last_retry_alert_mono > 0 and (now_mono - slot.last_retry_alert_mono) < interval:
            return
        slot.last_retry_alert_mono = now_mono
        title = "analysis 重试提醒"
        text = "\n".join(
            [
                "# analysis 重试提醒",
                "",
                f"- 课程：{slot.course_title} | {slot.teacher}",
                f"- 时间：{datetime.now(self._tz).strftime('%Y-%m-%d %H:%M:%S')}",
                f"- reason={reason}",
                f"- restart_total={slot.restart_total}, start_attempt_total={slot.start_attempt_total}",
            ]
        )
        self._send_dingtalk(title=title, text=text, slot=slot)

    def _send_dingtalk(self, *, title: str, text: str, slot: CourseSlotRuntime) -> None:
        ok, err = self.notifier.send_markdown(title=title, text=text)
        if ok:
            self.log(f"[slot] dingtalk sent label={slot.label()} title={title}")
        else:
            self.log(f"[slot] dingtalk failed label={slot.label()} title={title} error={err}")

    def _shutdown_all_processes(self) -> None:
        for slot in self.slots:
            controller = self._controllers[slot.slot_id]
            if controller.is_running():
                controller.stop(reason="scheduler_shutdown")

    def _build_analysis_command(self, *, course_id: int, sub_id: str) -> list[str]:
        cmd = [
            sys.executable,
            "-m",
            "src.main",
            "analysis",
            "--course-id",
            str(course_id),
            "--sub-id",
            str(sub_id),
        ]
        if str(getattr(self.args, "username", "") or "").strip():
            cmd.extend(["--username", str(self.args.username).strip()])
        if str(getattr(self.args, "password", "") or "").strip():
            cmd.extend(["--password", str(self.args.password).strip()])
        if str(getattr(self.args, "tenant_code", "") or "").strip():
            cmd.extend(["--tenant-code", str(self.args.tenant_code).strip()])
        if str(getattr(self.args, "authcode", "") or "").strip():
            cmd.extend(["--authcode", str(self.args.authcode).strip()])
        if getattr(self.args, "timeout", None) is not None:
            cmd.extend(["--timeout", str(int(self.args.timeout))])

        cmd.extend(_analysis_args_to_tokens(self._analysis_args))
        return cmd

    def _slot_guard_end(self, slot: CourseSlotRuntime) -> datetime:
        return slot.end_at + timedelta(minutes=max(0, self._runtime.post_end_guard_minutes))

    def _maybe_refresh_token(self) -> None:
        now_mono = time.monotonic()
        if self._token_refresh_last_mono > 0 and (now_mono - self._token_refresh_last_mono) < 600.0:
            return
        ok, error = self.token_manager.refresh("auto_periodic_refresh", force=False)
        self._token_refresh_last_mono = now_mono
        if not ok:
            self.log(f"[auto-analysis] token refresh skipped/failed: {error}")


def _compact_text(text: str, *, max_len: int = 180) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= max_len:
        return compact
    return compact[: max(1, max_len - 1)].rstrip() + "…"


def _notify_tingwu_precheck_billing_alert(
    *,
    notifier: DingTalkMarkdownSender,
    log_fn,
    error_text: str,
    course_count: int,
) -> None:
    raw_error = str(error_text or "").strip()
    if not raw_error:
        return
    hint = ""
    low = raw_error.lower()
    if "oss probe failed" in low:
        hint = "oss"
    elif "tingwu auth probe failed" in low:
        hint = "tingwu"
    issue = detect_billing_issue(service_hint=hint, error_text=raw_error)
    if issue is None:
        return

    allowed, remain_sec = consume_billing_alert_cooldown(issue.service_key)
    if not allowed:
        log_fn(
            f"[auto-analysis] billing alert skipped service={issue.service_key} "
            f"cooldown_remain={remain_sec:.1f}s"
        )
        return
    title = f"{issue.display_name} 欠费告警"
    text = "\n".join(
        [
            f"# {issue.display_name} 欠费告警",
            "",
            "- 场景：auto-analysis 启动前听悟远端预检失败",
            f"- 影响课程数：{max(0, int(course_count))}",
            f"- 命中信号：{issue.matched_signal}",
            f"- 冷却：{int(BILLING_ALERT_COOLDOWN_SEC)} 秒（按服务）",
            f"- 缴费入口：{issue.payment_url}",
            f"- 错误：{_compact_text(raw_error, max_len=300)}",
        ]
    )
    ok, err = notifier.send_markdown(title=title, text=text)
    if ok:
        log_fn(f"[auto-analysis] billing alert sent service={issue.service_key}")
    else:
        log_fn(f"[auto-analysis] billing alert failed service={issue.service_key} error={err}")


def run_auto_analysis(args: argparse.Namespace) -> int:
    config_path = Path(str(getattr(args, "config", "") or "")).expanduser().resolve()
    if not config_path.exists():
        print(f"Auto-analysis failed: config not found: {config_path}", file=sys.stderr)
        return 1

    try:
        config = load_auto_analysis_config(config_path)
    except ValueError as exc:
        print(f"Auto-analysis failed: invalid config: {exc}", file=sys.stderr)
        return 1

    parser_error = _validate_analysis_args_map(config.analysis_args)
    if parser_error:
        print(f"Auto-analysis failed: invalid analysis_args: {parser_error}", file=sys.stderr)
        return 1

    username, password, cred_error = resolve_credentials(args.username, args.password)
    if cred_error:
        print(f"Credential error: {cred_error}", file=sys.stderr)
        return 1

    instance_lock = AutoAnalysisInstanceLock(config_path=config_path)
    lock_ok, lock_detail = instance_lock.acquire()
    if not lock_ok:
        detail = lock_detail or f"lock_file={instance_lock.lock_path}"
        print(
            "Auto-analysis failed: another instance is already running "
            f"({detail})",
            file=sys.stderr,
        )
        return 1

    stop_event = threading.Event()
    stop_reason_ref: dict[str, str] = {"reason": ""}
    signal_installed = False
    previous_sigint = None
    previous_sigterm = None

    if threading.current_thread() is threading.main_thread():
        previous_sigint = signal.getsignal(signal.SIGINT)
        previous_sigterm = signal.getsignal(signal.SIGTERM)

        def _handle_stop_signal(signum, _frame) -> None:
            if stop_event.is_set():
                return
            try:
                signame = signal.Signals(int(signum)).name.lower()
            except Exception:
                signame = str(signum)
            stop_reason_ref["reason"] = f"signal_{signame}"
            stop_event.set()

        signal.signal(signal.SIGINT, _handle_stop_signal)
        signal.signal(signal.SIGTERM, _handle_stop_signal)
        signal_installed = True

    try:
        output_root = _resolve_output_root(config.analysis_args)
        run_dir = output_root / f"auto_analysis_{datetime.now(_SH_TZ).strftime('%Y%m%d_%H%M%S')}"
        run_dir.mkdir(parents=True, exist_ok=True)
        log_queue = AutoLogQueue(path=run_dir / "auto_analysis.log")
        log = log_queue.log
        log(f"[auto-analysis] config={config_path}")
        log(f"[auto-analysis] run_dir={run_dir}")

        try:
            auth = ZJUAuthClient(timeout=int(args.timeout), tenant_code=str(args.tenant_code))
            token_manager = LoginTokenManager(
                auth_client=auth,
                username=username,
                password=password,
                center_course_id=int(config.scan.center),
                authcode=str(args.authcode or ""),
                refresh_cooldown_sec=30.0,
                session_factory=lambda: create_session(pool_size=8),
            )
            ok, token_error = token_manager.refresh("initial_login", force=True)
            if not ok:
                log(f"[auto-analysis] login failed: {token_error}")
                return 1

            validation_errors, validated_meta = _validate_configured_courses(
                config=config,
                token=token_manager.get_token(),
                timeout=int(args.timeout),
                retries=int(config.scan.retries),
            )
            if validation_errors:
                log(f"[auto-analysis] precheck failed total_errors={len(validation_errors)}")
                for line in validation_errors:
                    log(f"[auto-analysis] precheck error: {line}")
                return 1

            webhook, secret, dingtalk_error = resolve_dingtalk_bot_settings()
            if dingtalk_error:
                log(f"[auto-analysis] dingtalk config error: {dingtalk_error}")
                return 1
            notifier = DingTalkMarkdownSender(
                webhook=webhook,
                secret=secret,
                timeout_sec=5.0,
                retry_count=3,
            )

            tingwu_enabled = bool(config.analysis_args.get("tingwu_enabled", False))
            if tingwu_enabled:
                local_error = validate_tingwu_local_requirements()
                if local_error:
                    log(f"[auto-analysis] tingwu precheck failed(local): {local_error}")
                    return 1
                ok, remote_error = run_tingwu_remote_preflight(timeout_sec=max(5.0, float(args.timeout)))
                if not ok:
                    log(f"[auto-analysis] tingwu precheck failed(remote): {remote_error}")
                    _notify_tingwu_precheck_billing_alert(
                        notifier=notifier,
                        log_fn=log,
                        error_text=remote_error,
                        course_count=len(config.courses),
                    )
                    return 1
                log("[auto-analysis] tingwu precheck ok (auth + oss probe)")

            for course in config.courses:
                meta = validated_meta.get(int(course.course_id))
                teachers = ",".join(meta[1]) if meta is not None else ""
                log(
                    f"[auto-analysis] precheck ok course_id={course.course_id} "
                    f"title={course.title} teacher={course.teacher} teachers={teachers}"
                )

            slots = _build_slot_runtime(config=config)
            scheduler = AutoAnalysisScheduler(
                args=args,
                config=config,
                token_manager=token_manager,
                notifier=notifier,
                slots=slots,
                log_queue=log_queue,
                stop_event=stop_event,
                stop_reason_ref=stop_reason_ref,
            )
            return scheduler.run()
        finally:
            log_queue.close()
    finally:
        if signal_installed:
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)
        instance_lock.release()


def load_auto_analysis_config(path: Path) -> AutoAnalysisConfig:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read config file: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"config must be valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("config root must be JSON object")

    timezone_name = str(payload.get("timezone", _DEFAULT_TIMEZONE) or _DEFAULT_TIMEZONE).strip()
    if timezone_name != _DEFAULT_TIMEZONE:
        raise ValueError(f"timezone must be {_DEFAULT_TIMEZONE}")
    tz = ZoneInfo(timezone_name)

    scan_raw = payload.get("scan")
    if scan_raw is None:
        scan_raw = {}
    if not isinstance(scan_raw, dict):
        raise ValueError("scan must be object")
    scan_cfg = AutoScanConfig(
        center=int(scan_raw.get("center", 82000)),
        radius=max(0, int(scan_raw.get("radius", 10000))),
        workers=max(1, int(scan_raw.get("workers", 64))),
        retries=max(0, int(scan_raw.get("retries", 1))),
        show_progress=bool(scan_raw.get("show_progress", True)),
        stop_when_all_found=bool(scan_raw.get("stop_when_all_found", True)),
    )

    runtime_raw = payload.get("runtime")
    if runtime_raw is None:
        runtime_raw = {}
    if not isinstance(runtime_raw, dict):
        raise ValueError("runtime must be object")
    runtime_cfg = AutoRuntimeConfig(
        pre_start_notice_minutes=max(0, int(runtime_raw.get("pre_start_notice_minutes", 15))),
        near_start_probe_interval_sec=max(0.2, float(runtime_raw.get("near_start_probe_interval_sec", 2))),
        after_start_probe_interval_sec=max(0.2, float(runtime_raw.get("after_start_probe_interval_sec", 2))),
        late_probe_interval_sec=max(0.2, float(runtime_raw.get("late_probe_interval_sec", 30))),
        near_end_probe_interval_sec=max(0.2, float(runtime_raw.get("near_end_probe_interval_sec", 2))),
        post_end_guard_minutes=max(0, int(runtime_raw.get("post_end_guard_minutes", 15))),
        no_live_alert_interval_sec=max(1.0, float(runtime_raw.get("no_live_alert_interval_sec", 30))),
        no_live_alert_duration_minutes=max(0, int(runtime_raw.get("no_live_alert_duration_minutes", 15))),
        retry_alert_min_interval_sec=max(0.0, float(runtime_raw.get("retry_alert_min_interval_sec", 30))),
        main_tick_sec=max(0.2, float(runtime_raw.get("main_tick_sec", 1))),
    )

    analysis_args_raw = payload.get("analysis_args")
    if analysis_args_raw is None:
        analysis_args_raw = {}
    if not isinstance(analysis_args_raw, dict):
        raise ValueError("analysis_args must be object")
    analysis_args = dict(analysis_args_raw)

    courses_raw = payload.get("courses")
    if not isinstance(courses_raw, list) or not courses_raw:
        raise ValueError("courses must be a non-empty array")

    deduped: dict[tuple[int, str, str], AutoCourseSpec] = {}
    course_id_owners: dict[int, tuple[str, str]] = {}
    for index, raw_item in enumerate(courses_raw, start=1):
        if not isinstance(raw_item, dict):
            raise ValueError(f"courses[{index}] must be object")
        raw_course_id = raw_item.get("course_id", None)
        if raw_course_id is None or isinstance(raw_course_id, bool):
            raise ValueError(f"courses[{index}] course_id is required")
        try:
            course_id = int(str(raw_course_id).strip())
        except (TypeError, ValueError):
            raise ValueError(f"courses[{index}] course_id must be an integer") from None
        if course_id <= 0:
            raise ValueError(f"courses[{index}] course_id must be > 0")
        title = str(raw_item.get("title", "") or "").strip()
        teacher = str(raw_item.get("teacher", "") or "").strip()
        if not title:
            raise ValueError(f"courses[{index}] title is required")
        if not teacher:
            raise ValueError(f"courses[{index}] teacher is required")

        owner = course_id_owners.get(course_id)
        if owner is None:
            course_id_owners[course_id] = (title, teacher)
        elif owner != (title, teacher):
            raise ValueError(
                f"course_id conflict: course_id={course_id} maps to both "
                f"({owner[0]}, {owner[1]}) and ({title}, {teacher})"
            )

        slots_raw = raw_item.get("slots")
        if not isinstance(slots_raw, list) or not slots_raw:
            raise ValueError(f"courses[{index}] slots must be non-empty array")

        slot_list: list[AutoCourseSlot] = []
        for slot_index, slot_payload in enumerate(slots_raw, start=1):
            if not isinstance(slot_payload, dict):
                raise ValueError(f"courses[{index}].slots[{slot_index}] must be object")
            start_at = _parse_local_datetime(slot_payload.get("start"), tz=tz)
            end_at = _parse_local_datetime(slot_payload.get("end"), tz=tz)
            if start_at >= end_at:
                raise ValueError(
                    f"courses[{index}].slots[{slot_index}] requires start < end"
                )
            slot_list.append(AutoCourseSlot(start=start_at, end=end_at))

        key = (course_id, title, teacher)
        existing = deduped.get(key)
        if existing is None:
            deduped[key] = AutoCourseSpec(
                course_id=course_id,
                title=title,
                teacher=teacher,
                slots=list(slot_list),
            )
        else:
            existing.slots.extend(slot_list)

    courses = list(deduped.values())
    for course in courses:
        course.slots.sort(key=lambda x: x.start)
        for idx in range(1, len(course.slots)):
            prev = course.slots[idx - 1]
            cur = course.slots[idx]
            if cur.start < prev.end:
                raise ValueError(
                    f"overlapped slots for course={course.title} teacher={course.teacher} "
                    f"at {prev.start.isoformat()} and {cur.start.isoformat()}"
                )

    return AutoAnalysisConfig(
        timezone=timezone_name,
        scan=scan_cfg,
        runtime=runtime_cfg,
        analysis_args=analysis_args,
        courses=courses,
    )


def _parse_local_datetime(value: object, *, tz: ZoneInfo) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("slot datetime cannot be empty")

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise ValueError(f"unsupported datetime format: {text}")

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _analysis_args_to_tokens(analysis_args: dict[str, Any]) -> list[str]:
    tokens: list[str] = []
    for key in sorted(analysis_args.keys()):
        cli_key = f"--{str(key).replace('_', '-')}"
        value = analysis_args[key]
        if isinstance(value, bool):
            if value:
                tokens.append(cli_key)
            continue
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            tokens.extend([cli_key, ",".join(str(item) for item in value)])
            continue
        if isinstance(value, str) and value == "":
            continue
        tokens.extend([cli_key, str(value)])
    return tokens


def _validate_analysis_args_map(analysis_args: dict[str, Any]) -> str:
    forbidden = {
        "course_id",
        "sub_id",
        "command",
        "config",
        "username",
        "password",
        "tenant_code",
        "timeout",
        "authcode",
    }
    for key in analysis_args.keys():
        if str(key) in forbidden:
            return f"analysis_args cannot include {key}"

    parser = build_parser()
    tokens = [
        "analysis",
        "--course-id",
        "1",
        "--sub-id",
        "1",
    ] + _analysis_args_to_tokens(analysis_args)
    try:
        parsed = parser.parse_args(tokens)
    except SystemExit:
        return "contains unknown option or invalid option value"

    validation_error = _validate_analysis_args(parsed)
    if validation_error:
        return validation_error
    return ""


def _build_slot_runtime(
    *,
    config: AutoAnalysisConfig,
) -> list[CourseSlotRuntime]:
    slots: list[CourseSlotRuntime] = []
    for course in config.courses:
        for index, slot in enumerate(course.slots, start=1):
            slot_id = (
                f"{course.course_id}|{course.title}|{course.teacher}|"
                f"{slot.start.strftime('%Y%m%d%H%M%S')}|{index}"
            )
            slots.append(
                CourseSlotRuntime(
                    slot_id=slot_id,
                    course_title=course.title,
                    teacher=course.teacher,
                    course_id=int(course.course_id),
                    start_at=slot.start,
                    end_at=slot.end,
                )
            )
    slots.sort(key=lambda item: item.start_at)
    return slots


def _resolve_output_root(analysis_args: dict[str, Any]) -> Path:
    raw = str(analysis_args.get("output_dir", "") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.cwd()


def _validate_configured_courses(
    *,
    config: AutoAnalysisConfig,
    token: str,
    timeout: int,
    retries: int,
) -> tuple[list[str], dict[int, tuple[str, list[str]]]]:
    errors: list[str] = []
    cache: dict[int, tuple[str, list[str]]] = {}
    session = create_session(pool_size=8)
    try:
        for course in config.courses:
            course_id = int(course.course_id)
            meta = cache.get(course_id)
            if meta is None:
                data = query_course_detail(
                    session=session,
                    token=token,
                    timeout=int(timeout),
                    course_id=course_id,
                    retries=max(0, int(retries)),
                )
                if not data:
                    errors.append(
                        f"course_id={course_id} title={course.title} teacher={course.teacher} "
                        "detail unavailable"
                    )
                    continue
                fetched_title = str(data.get("title") or "").strip()
                fetched_teachers = course_teachers(data)
                cache[course_id] = (fetched_title, fetched_teachers)
                meta = cache[course_id]

            fetched_title, fetched_teachers = meta
            if fetched_title != course.title:
                errors.append(
                    f"course_id={course_id} title mismatch config={course.title} fetched={fetched_title}"
                )
                continue
            if course.teacher not in fetched_teachers:
                errors.append(
                    f"course_id={course_id} teacher mismatch config={course.teacher} "
                    f"fetched={','.join(fetched_teachers)}"
                )
    finally:
        try:
            session.close()
        except Exception:
            pass

    return errors, cache
