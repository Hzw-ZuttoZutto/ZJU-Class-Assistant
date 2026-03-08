from __future__ import annotations

import base64
import hashlib
import hmac
import queue
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from urllib.parse import quote_plus

from src.common.http import get_thread_session
from src.live.insight.models import InsightEvent

_CHUNK_TS_PATTERN = re.compile(r"(\d{8}_\d{6})")


@dataclass(frozen=True)
class DingTalkNotifierMetadata:
    course_title: str = ""
    teacher_name: str = ""


class DingTalkNotifier:
    def __init__(
        self,
        *,
        webhook: str,
        secret: str,
        cooldown_sec: float = 30.0,
        send_timeout_sec: float = 5.0,
        send_retry_count: int = 5,
        metadata: DingTalkNotifierMetadata | None = None,
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.webhook = (webhook or "").strip()
        self.secret = (secret or "").strip()
        self.cooldown_sec = max(0.0, float(cooldown_sec))
        self.send_timeout_sec = max(1.0, float(send_timeout_sec))
        self.send_retry_count = max(1, int(send_retry_count))
        self.metadata = metadata or DingTalkNotifierMetadata()
        self._log_fn = log_fn or print

        self._queue: queue.Queue[InsightEvent | None] = queue.Queue()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._last_accepted_at = 0.0

        if not self.webhook:
            raise ValueError("DingTalk webhook is empty")
        if not self.secret:
            raise ValueError("DingTalk secret is empty")

    def notify_event(self, event: InsightEvent) -> bool:
        if not event.important:
            return False
        if not self._accept_by_cooldown():
            self._log(
                f"[rt-dingtalk] cooldown skip seq={event.chunk_seq} chunk={event.chunk_file} "
                f"window={self.cooldown_sec:.1f}s"
            )
            return False
        self._ensure_worker()
        self._queue.put(event)
        return True

    def stop(self) -> None:
        self._stop_event.set()
        worker = self._worker
        if worker is None:
            return
        self._queue.put(None)
        worker.join(timeout=5.0)
        self._worker = None

    def _ensure_worker(self) -> None:
        with self._state_lock:
            worker = self._worker
            if worker is not None and worker.is_alive():
                return
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._run,
                name="rt-dingtalk-notifier",
                daemon=True,
            )
            self._worker.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._deliver_event(item)
            finally:
                self._queue.task_done()

    def _accept_by_cooldown(self) -> bool:
        now = time.monotonic()
        with self._state_lock:
            if self._last_accepted_at > 0 and (now - self._last_accepted_at) < self.cooldown_sec:
                return False
            self._last_accepted_at = now
            return True

    def _deliver_event(self, event: InsightEvent) -> None:
        payload = self._build_payload(event)
        last_error = ""
        for attempt in range(1, self.send_retry_count + 1):
            try:
                self._send_payload(payload)
                self._log(
                    f"[rt-dingtalk] sent seq={event.chunk_seq} chunk={event.chunk_file} "
                    f"attempt={attempt} recovery={event.is_recovery}"
                )
                return
            except Exception as exc:
                last_error = str(exc)
                if attempt >= self.send_retry_count:
                    break
                delay_sec = min(16.0, float(2 ** (attempt - 1)))
                if self._wait_backoff(delay_sec):
                    return
        self._log(
            f"[rt-dingtalk] send failed seq={event.chunk_seq} chunk={event.chunk_file} "
            f"attempts={self.send_retry_count} error={last_error}"
        )

    def _send_payload(self, payload: dict) -> None:
        timestamp_ms = int(time.time() * 1000)
        signed_url = self._build_signed_webhook_url(timestamp_ms)
        resp = get_thread_session(pool_size=8).post(
            signed_url,
            json=payload,
            timeout=self.send_timeout_sec,
        )
        resp.raise_for_status()
        try:
            body = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"invalid DingTalk response: {resp.text[:200]}") from exc
        if int(body.get("errcode", -1)) != 0:
            raise RuntimeError(f"errcode={body.get('errcode')} errmsg={body.get('errmsg', '')}")

    def _build_signed_webhook_url(self, timestamp_ms: int) -> str:
        string_to_sign = f"{int(timestamp_ms)}\n{self.secret}"
        digest = hmac.new(
            self.secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = quote_plus(base64.b64encode(digest).decode("utf-8"))
        separator = "&" if "?" in self.webhook else "?"
        return f"{self.webhook}{separator}timestamp={int(timestamp_ms)}&sign={sign}"

    def _build_payload(self, event: InsightEvent) -> dict:
        title = "[补发] 紧急" if event.is_recovery else "紧急"
        text = self._build_markdown_text(event)
        return {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": text,
            },
        }

    def _build_markdown_text(self, event: InsightEvent) -> str:
        heading = "# [补发] 紧急" if event.is_recovery else "# 紧急"
        lines = [heading, ""]
        course_line = self._course_line()
        if course_line:
            lines.append(f"- 课程：{course_line}")
        lines.extend(
            [
                f"- 事件时间：{self._event_time_text(event)}",
                f"- summary: {event.summary}",
                f"- context_summary: {event.context_summary}",
                f"- reason: {event.reason}",
            ]
        )
        return "\n".join(lines)

    def _course_line(self) -> str:
        parts = [self.metadata.course_title.strip(), self.metadata.teacher_name.strip()]
        parts = [item for item in parts if item]
        return " | ".join(parts)

    def _event_time_text(self, event: InsightEvent) -> str:
        chunk_ts = self._parse_chunk_timestamp(event.chunk_file)
        if chunk_ts is not None:
            return chunk_ts.strftime("%Y-%m-%d %H:%M:%S")
        return event.ts.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _parse_chunk_timestamp(chunk_file: str) -> datetime | None:
        match = _CHUNK_TS_PATTERN.search(str(chunk_file or ""))
        if match is None:
            return None
        try:
            return datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
        except ValueError:
            return None

    def _wait_backoff(self, delay_sec: float) -> bool:
        return self._stop_event.wait(max(0.0, float(delay_sec)))

    def _log(self, msg: str) -> None:
        self._log_fn(msg)
