from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from src.live.insight.dingtalk import DingTalkNotifier, DingTalkNotifierMetadata
from src.live.insight.models import InsightEvent


class _DingTalkHandler(BaseHTTPRequestHandler):
    request_count = 0
    fail_before_success = 0
    last_path = ""
    last_payload: dict | None = None

    def do_POST(self) -> None:  # noqa: N802
        type(self).request_count += 1
        type(self).last_path = self.path
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length)
        type(self).last_payload = json.loads(raw.decode("utf-8"))

        if type(self).request_count <= type(self).fail_before_success:
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"errcode":500,"errmsg":"fail"}')
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'{"errcode":0,"errmsg":"ok"}')

    def log_message(self, fmt: str, *args: object) -> None:
        return


class DingTalkNotifierTests(unittest.TestCase):
    def _event(self, *, important: bool = True, recovery: bool = False, chunk_file: str = "chunk_20260101_010203.mp3") -> InsightEvent:
        return InsightEvent(
            ts=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            chunk_seq=7,
            chunk_file=chunk_file,
            model="gpt-5-mini",
            important=important,
            summary="有紧急签到",
            context_summary="老师要求立刻打开手机签到",
            event_type="sign_in",
            headline="立即签到",
            immediate_action="现在打开手机完成签到",
            key_details=["签到码：1234", "使用课堂派链接进入"],
            matched_terms=["签到"],
            reason="keyword_hit",
            attempt_count=1,
            context_chunk_count=3,
            is_recovery=recovery,
        )

    def test_build_payload_contains_course_and_recovery_title(self) -> None:
        notifier = DingTalkNotifier(
            webhook="https://example.test/robot/send?access_token=x",
            secret="sec-123",
            metadata=DingTalkNotifierMetadata(course_title="高等数学", teacher_name="张老师"),
            log_fn=lambda _msg: None,
        )

        payload = notifier._build_payload(self._event(recovery=True))
        markdown = payload["markdown"]

        self.assertEqual(payload["msgtype"], "markdown")
        self.assertEqual(markdown["title"], "【补发】立即签到")
        self.assertIn("# 【补发】立即签到", markdown["text"])
        self.assertIn("课程：高等数学 | 张老师", markdown["text"])
        self.assertIn("事件时间：2026-01-01 01:02:03", markdown["text"])
        self.assertIn("## 当前最紧急", markdown["text"])
        self.assertIn("有紧急签到", markdown["text"])
        self.assertIn("## 现在就做", markdown["text"])
        self.assertIn("现在打开手机完成签到", markdown["text"])
        self.assertIn("## 关键细节", markdown["text"])
        self.assertIn("签到码：1234", markdown["text"])
        self.assertIn("## 判断依据", markdown["text"])
        self.assertIn("老师要求立刻打开手机签到", markdown["text"])
        self.assertNotIn("reason:", markdown["text"])
        self.assertNotIn("matched_terms", markdown["text"])

    def test_build_payload_omits_course_line_without_metadata(self) -> None:
        notifier = DingTalkNotifier(
            webhook="https://example.test/robot/send?access_token=x",
            secret="sec-123",
            log_fn=lambda _msg: None,
        )

        payload = notifier._build_payload(self._event())
        self.assertNotIn("课程：", payload["markdown"]["text"])

    def test_build_payload_omits_key_detail_section_when_empty(self) -> None:
        notifier = DingTalkNotifier(
            webhook="https://example.test/robot/send?access_token=x",
            secret="sec-123",
            log_fn=lambda _msg: None,
        )

        event = self._event()
        event.key_details = []
        payload = notifier._build_payload(event)
        self.assertNotIn("## 关键细节", payload["markdown"]["text"])

    def test_build_signed_webhook_url_contains_expected_query(self) -> None:
        notifier = DingTalkNotifier(
            webhook="https://example.test/robot/send?access_token=x",
            secret="sec-123",
            log_fn=lambda _msg: None,
        )

        signed_url = notifier._build_signed_webhook_url(1700000000000)
        parsed = urlparse(signed_url)
        query = parse_qs(parsed.query)

        self.assertEqual(query["access_token"], ["x"])
        self.assertEqual(query["timestamp"], ["1700000000000"])
        self.assertTrue(query["sign"][0])

    def test_notify_event_respects_cooldown(self) -> None:
        notifier = DingTalkNotifier(
            webhook="https://example.test/robot/send?access_token=x",
            secret="sec-123",
            cooldown_sec=30.0,
            log_fn=lambda _msg: None,
        )

        with mock.patch.object(notifier, "_ensure_worker", return_value=None), mock.patch(
            "src.live.insight.dingtalk.time.monotonic",
            side_effect=[100.0, 120.0, 131.0],
        ):
            self.assertTrue(notifier.notify_event(self._event()))
            self.assertFalse(notifier.notify_event(self._event()))
            self.assertTrue(notifier.notify_event(self._event()))

        self.assertEqual(notifier._queue.qsize(), 2)

    def test_notify_event_keeps_trace_context(self) -> None:
        notifier = DingTalkNotifier(
            webhook="https://example.test/robot/send?access_token=x",
            secret="sec-123",
            cooldown_sec=30.0,
            log_fn=lambda _msg: None,
        )

        with mock.patch.object(notifier, "_ensure_worker", return_value=None), mock.patch(
            "src.live.insight.dingtalk.time.monotonic",
            return_value=100.0,
        ):
            accepted = notifier.notify_event(
                self._event(),
                pre_send_ts_ms=2000,
                pre_send_rel_ms=450,
                stream_t0_ms=1550,
            )
        self.assertTrue(accepted)
        queued = notifier._queue.get_nowait()
        assert queued is not None
        event, ctx = queued
        self.assertEqual(event.chunk_seq, 7)
        self.assertEqual(ctx["pre_send_ts_ms"], 2000)
        self.assertEqual(ctx["pre_send_rel_ms"], 450)
        self.assertEqual(ctx["stream_t0_ms"], 1550)

    def test_deliver_event_writes_trace_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            trace_path = Path(td) / "realtime_dingtalk_trace.jsonl"
            notifier = DingTalkNotifier(
                webhook="https://example.test/robot/send?access_token=x",
                secret="sec-123",
                send_retry_count=2,
                trace_path=trace_path,
                log_fn=lambda _msg: None,
            )

            with mock.patch.object(notifier, "_send_payload", return_value=None):
                notifier._deliver_event(
                    self._event(),
                    trace_context={"pre_send_ts_ms": 5000, "pre_send_rel_ms": 300, "stream_t0_ms": 4700},
                )

            with mock.patch.object(notifier, "_send_payload", side_effect=RuntimeError("boom")), mock.patch.object(
                notifier,
                "_wait_backoff",
                return_value=False,
            ):
                notifier._deliver_event(
                    self._event(chunk_file="chunk_20260101_020304.mp3"),
                    trace_context={"pre_send_ts_ms": 8000, "pre_send_rel_ms": 600, "stream_t0_ms": 7400},
                )

            rows = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["status"], "sent")
            self.assertEqual(rows[0]["attempt_count"], 1)
            self.assertEqual(rows[0]["asr_sentence_id"], "")
            self.assertEqual(rows[0]["pre_send_rel_ms"], 300)
            self.assertEqual(rows[1]["status"], "failed")
            self.assertEqual(rows[1]["attempt_count"], 2)
            self.assertIn("boom", rows[1]["error"])

    def test_deliver_event_retries_five_times(self) -> None:
        _DingTalkHandler.request_count = 0
        _DingTalkHandler.fail_before_success = 4
        _DingTalkHandler.last_path = ""
        _DingTalkHandler.last_payload = None

        server = ThreadingHTTPServer(("127.0.0.1", 0), _DingTalkHandler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        thread.start()
        try:
            notifier = DingTalkNotifier(
                webhook=f"http://127.0.0.1:{server.server_port}/robot/send?access_token=x",
                secret="sec-123",
                send_retry_count=5,
                log_fn=lambda _msg: None,
            )
            delays: list[float] = []
            with mock.patch.object(
                notifier,
                "_wait_backoff",
                side_effect=lambda delay_sec: delays.append(float(delay_sec)) or False,
            ):
                notifier._deliver_event(self._event())
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(_DingTalkHandler.request_count, 5)
        self.assertEqual(delays, [1.0, 2.0, 4.0, 8.0])
        self.assertIsNotNone(_DingTalkHandler.last_payload)
        self.assertEqual(_DingTalkHandler.last_payload["msgtype"], "markdown")

    def test_trace_log_rotates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            trace_path = Path(td) / "realtime_dingtalk_trace.jsonl"
            notifier = DingTalkNotifier(
                webhook="https://example.test/robot/send?access_token=x",
                secret="sec-123",
                send_retry_count=1,
                trace_path=trace_path,
                log_rotate_max_bytes=128,
                log_rotate_backup_count=2,
                log_fn=lambda _msg: None,
            )
            with mock.patch.object(notifier, "_send_payload", return_value=None):
                for _ in range(20):
                    notifier._deliver_event(
                        self._event(chunk_file="chunk_20260101_020304.mp3"),
                        trace_context={"pre_send_ts_ms": 8000, "pre_send_rel_ms": 600, "stream_t0_ms": 7400},
                    )
            self.assertTrue((Path(td) / "realtime_dingtalk_trace.jsonl.1").exists())
