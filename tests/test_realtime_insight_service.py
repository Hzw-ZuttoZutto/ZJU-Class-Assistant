from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.live.insight.models import RealtimeInsightConfig
from src.live.insight.openai_client import InsightModelResult
from src.live.insight.service import RealtimeInsightService


class _FakeStream:
    def __init__(self, url: str) -> None:
        self.stream_m3u8 = url


class _FakeSnapshot:
    def __init__(self, url: str) -> None:
        self.streams = {"teacher": _FakeStream(url)} if url else {}


class _FakePoller:
    def __init__(self, url: str) -> None:
        self.url = url

    def get_snapshot(self):
        return _FakeSnapshot(self.url)


class _FakeChunker:
    def __init__(self) -> None:
        self.started_url = ""
        self.stopped = False

    def ensure_available(self) -> bool:
        return True

    def start(self, stream_url: str) -> None:
        self.started_url = stream_url

    def stop(self, grace_sec: float = 2.0) -> None:
        self.stopped = True


class _FakeClient:
    def __init__(
        self,
        *,
        timeout_on_stt: bool = False,
        timeout_on_analysis: bool = False,
        important: bool = True,
    ) -> None:
        self.timeout_on_stt = timeout_on_stt
        self.timeout_on_analysis = timeout_on_analysis
        self.important = important
        self.analysis_contexts: list[str] = []

    def transcribe_chunk(self, *, chunk_path: Path, stt_model: str, timeout_sec: float) -> str:
        if self.timeout_on_stt:
            raise TimeoutError("stt timeout")
        return f"transcript-{chunk_path.name}"

    def analyze_text(
        self,
        *,
        analysis_model: str,
        keywords,
        current_text: str,
        context_text: str,
        timeout_sec: float,
    ):
        if self.timeout_on_analysis:
            raise TimeoutError("analysis timeout")
        self.analysis_contexts.append(context_text)
        return InsightModelResult(
            important=self.important,
            summary="提到了微积分重点",
            context_summary="老师在讲导数与极限关系",
            matched_terms=["微积分", "导数"],
            reason="keyword_hit",
        )


class _FakeNotifier:
    def __init__(self, *, should_raise: bool = False) -> None:
        self.should_raise = should_raise
        self.events = []
        self.stopped = False

    def notify_event(self, event) -> bool:
        if self.should_raise:
            raise RuntimeError("notify failed")
        if not bool(getattr(event, "important", False)):
            return False
        self.events.append(event)
        return True

    def stop(self) -> None:
        self.stopped = True


class RealtimeInsightServiceTests(unittest.TestCase):
    def _build_service(
        self,
        session_dir: Path,
        client: _FakeClient,
        notifier: _FakeNotifier | None = None,
    ) -> RealtimeInsightService:
        config = RealtimeInsightConfig(
            enabled=True,
            chunk_seconds=10,
            context_window_seconds=180,
            model="gpt-5-mini",
            stt_model="gpt-4o-mini-transcribe",
            stt_request_timeout_sec=5.0,
            stt_retry_count=0,
            stt_stage_timeout_sec=1.0,
            stt_retry_interval_sec=0.01,
            analysis_request_timeout_sec=5.0,
            analysis_retry_count=0,
            analysis_stage_timeout_sec=1.0,
            analysis_retry_interval_sec=0.01,
            context_wait_timeout_sec=0.05,
            context_min_ready=15,
            context_recent_required=4,
            context_target_chunks=18,
            context_wait_timeout_sec_1=0.0,
            context_wait_timeout_sec_2=0.05,
            use_dual_context_wait=True,
            max_concurrency=5,
            dingtalk_enabled=notifier is not None,
        )
        return RealtimeInsightService(
            poller=_FakePoller("https://x/live.m3u8"),
            session_dir=session_dir,
            config=config,
            chunker=_FakeChunker(),
            client=client,
            notifier=notifier,
            log_fn=lambda _: None,
        )

    def test_process_chunk_success_writes_transcript_and_insight(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            session_dir = Path(td)
            chunk = session_dir / "chunk_20260101_000000.mp3"
            chunk.write_bytes(b"audio")

            service = self._build_service(session_dir, _FakeClient())
            self.assertTrue(service._prepare_runtime())
            service._process_chunk_task(1, chunk)

            transcripts = (session_dir / "realtime_transcripts.jsonl").read_text(encoding="utf-8").strip()
            insights = (session_dir / "realtime_insights.jsonl").read_text(encoding="utf-8").strip()
            text_log = (session_dir / "realtime_insights.log").read_text(encoding="utf-8")

            transcript_payload = json.loads(transcripts)
            insight_payload = json.loads(insights)
            self.assertEqual(transcript_payload["status"], "ok")
            self.assertEqual(insight_payload["status"], "ok")
            self.assertEqual(insight_payload["chunk_seq"], 1)
            self.assertEqual(
                text_log,
                "紧急!\n具体内容：提到了微积分重点\n具体上下文：老师在讲导数与极限关系\n\n",
            )

    def test_process_chunk_non_urgent_text_log_uses_plain_label(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            session_dir = Path(td)
            chunk = session_dir / "chunk_20260101_000000.mp3"
            chunk.write_bytes(b"audio")

            service = self._build_service(session_dir, _FakeClient(important=False))
            self.assertTrue(service._prepare_runtime())
            service._process_chunk_task(1, chunk)

            text_log = (session_dir / "realtime_insights.log").read_text(encoding="utf-8")
            self.assertEqual(
                text_log,
                "平常\n具体内容：提到了微积分重点\n具体上下文：老师在讲导数与极限关系\n\n",
            )

    def test_process_chunk_important_event_enqueues_dingtalk_notification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            session_dir = Path(td)
            chunk = session_dir / "chunk_20260101_000000.mp3"
            chunk.write_bytes(b"audio")
            notifier = _FakeNotifier()

            service = self._build_service(session_dir, _FakeClient(important=True), notifier=notifier)
            self.assertTrue(service._prepare_runtime())
            service._process_chunk_task(1, chunk)

            self.assertEqual(len(notifier.events), 1)
            self.assertTrue(bool(notifier.events[0].important))

    def test_process_chunk_non_important_event_skips_dingtalk_notification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            session_dir = Path(td)
            chunk = session_dir / "chunk_20260101_000000.mp3"
            chunk.write_bytes(b"audio")
            notifier = _FakeNotifier()

            service = self._build_service(session_dir, _FakeClient(important=False), notifier=notifier)
            self.assertTrue(service._prepare_runtime())
            service._process_chunk_task(1, chunk)

            self.assertEqual(notifier.events, [])

    def test_dingtalk_enqueue_failure_does_not_break_local_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            session_dir = Path(td)
            chunk = session_dir / "chunk_20260101_000000.mp3"
            chunk.write_bytes(b"audio")
            notifier = _FakeNotifier(should_raise=True)

            service = self._build_service(session_dir, _FakeClient(important=True), notifier=notifier)
            self.assertTrue(service._prepare_runtime())
            service._process_chunk_task(1, chunk)

            insights = (session_dir / "realtime_insights.jsonl").read_text(encoding="utf-8").strip()
            payload = json.loads(insights)
            self.assertEqual(payload["status"], "ok")

    def test_transcript_timeout_drop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            session_dir = Path(td)
            chunk = session_dir / "chunk_20260101_000000.mp3"
            chunk.write_bytes(b"audio")

            service = self._build_service(session_dir, _FakeClient(timeout_on_stt=True))
            self.assertTrue(service._prepare_runtime())
            service._process_chunk_task(1, chunk)

            transcripts = (session_dir / "realtime_transcripts.jsonl").read_text(encoding="utf-8").strip()
            transcript_payload = json.loads(transcripts)
            self.assertEqual(transcript_payload["status"], "transcript_drop_timeout")
            self.assertFalse((session_dir / "realtime_insights.jsonl").exists())

    def test_context_wait_timeout_uses_partial_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            session_dir = Path(td)
            client = _FakeClient()
            service = self._build_service(session_dir, client)
            self.assertTrue(service._prepare_runtime())

            # only one chunk: context gate cannot be satisfied, should fallback after timeout
            chunk = session_dir / "chunk_20260101_000000.mp3"
            chunk.write_bytes(b"audio")
            service._process_chunk_task(1, chunk)

            self.assertTrue(client.analysis_contexts)
            self.assertIn("无历史文本块", client.analysis_contexts[-1])

    def test_analysis_timeout_drop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            session_dir = Path(td)
            chunk = session_dir / "chunk_20260101_000000.mp3"
            chunk.write_bytes(b"audio")

            service = self._build_service(session_dir, _FakeClient(timeout_on_analysis=True))
            self.assertTrue(service._prepare_runtime())
            service._process_chunk_task(1, chunk)

            insights = (session_dir / "realtime_insights.jsonl").read_text(encoding="utf-8").strip()
            payload = json.loads(insights)
            self.assertEqual(payload["status"], "analysis_drop_timeout")

    def test_dispatch_ready_chunks_submits_async_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            session_dir = Path(td)
            chunk_dir = session_dir / "_rt_chunks"
            chunk_dir.mkdir(parents=True, exist_ok=True)
            for idx in range(3):
                chunk = chunk_dir / f"chunk_20260101_00000{idx}.mp3"
                chunk.write_bytes(b"audio")

            service = self._build_service(session_dir, _FakeClient())
            self.assertTrue(service._prepare_runtime())
            service._executor = ThreadPoolExecutor(max_workers=2)
            try:
                service._dispatch_ready_chunks(force=True)
                self.assertEqual(len(service._futures), 3)
                service._wait_for_running_tasks()
            finally:
                service._executor.shutdown(wait=True, cancel_futures=False)
                service._executor = None

            transcript_lines = (session_dir / "realtime_transcripts.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(transcript_lines), 3)


if __name__ == "__main__":
    unittest.main()
