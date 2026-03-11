from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.common.billing import reset_billing_alert_cooldown_for_tests
from src.live.insight.models import KeywordConfig, RealtimeInsightConfig, TranscriptChunk
from src.live.insight.openai_client import InsightModelResult
from src.live.insight.stage_processor import InsightStageProcessor


class _RetryClient:
    def __init__(self) -> None:
        self.stt_calls = 0
        self.analysis_calls = 0
        self.stt_timeouts: list[float] = []
        self.analysis_timeouts: list[float] = []

    def transcribe_chunk(self, *, chunk_path: Path, stt_model: str, timeout_sec: float) -> str:
        self.stt_calls += 1
        self.stt_timeouts.append(float(timeout_sec))
        if self.stt_calls == 1:
            raise RuntimeError("stt fail once")
        return "转写成功"

    def analyze_text(
        self,
        *,
        analysis_model: str,
        keywords: KeywordConfig,
        current_text: str,
        context_text: str,
        chunk_seconds: float,
        timeout_sec: float,
        debug_hook=None,
    ) -> InsightModelResult:
        self.analysis_calls += 1
        self.analysis_timeouts.append(float(timeout_sec))
        if self.analysis_calls == 1:
            raise RuntimeError("analysis fail once")
        if debug_hook is not None:
            debug_hook(
                {
                    "chunk_seconds": chunk_seconds,
                    "current_text": current_text,
                    "context_text": context_text,
                    "system_prompt": "sys",
                    "user_prompt": "usr",
                    "request_payload_snapshot": {"model": analysis_model},
                    "raw_response_text": '{"important": false}',
                    "parsed_ok": True,
                    "parsed_payload": {"important": False},
                    "error": "",
                    "duration_sec": 0.01,
                }
            )
        return InsightModelResult(
            important=False,
            summary="ok",
            context_summary="ok",
            matched_terms=[],
            reason="ok",
        )


class _AlwaysOkClient:
    def transcribe_chunk(self, *, chunk_path: Path, stt_model: str, timeout_sec: float) -> str:
        return "startup-ok"

    def analyze_text(
        self,
        *,
        analysis_model: str,
        keywords: KeywordConfig,
        current_text: str,
        context_text: str,
        chunk_seconds: float,
        timeout_sec: float,
        debug_hook=None,
    ) -> InsightModelResult:
        if debug_hook is not None:
            debug_hook(
                {
                    "chunk_seconds": chunk_seconds,
                    "current_text": current_text,
                    "context_text": context_text,
                    "system_prompt": "sys",
                    "user_prompt": "usr",
                    "request_payload_snapshot": {"model": analysis_model},
                    "raw_response_text": '{"important": false}',
                    "parsed_ok": True,
                    "parsed_payload": {"important": False},
                    "error": "",
                    "duration_sec": 0.01,
                }
            )
        return InsightModelResult(
            important=False,
            summary="ok",
            context_summary="ok",
            matched_terms=[],
            reason="ok",
        )


class _AnalyzeBillingClient:
    def transcribe_chunk(self, *, chunk_path: Path, stt_model: str, timeout_sec: float) -> str:
        _ = (chunk_path, stt_model, timeout_sec)
        return "实时文本"

    def analyze_text(
        self,
        *,
        analysis_model: str,
        keywords: KeywordConfig,
        current_text: str,
        context_text: str,
        chunk_seconds: float,
        timeout_sec: float,
        debug_hook=None,
    ) -> InsightModelResult:
        _ = (analysis_model, keywords, current_text, context_text, chunk_seconds, timeout_sec, debug_hook)
        raise RuntimeError("429 insufficient_quota: You exceeded your current quota.")


class _TranscribeBillingClient:
    def transcribe_chunk(self, *, chunk_path: Path, stt_model: str, timeout_sec: float) -> str:
        _ = (chunk_path, stt_model, timeout_sec)
        raise RuntimeError("insufficient_quota")

    def analyze_text(
        self,
        *,
        analysis_model: str,
        keywords: KeywordConfig,
        current_text: str,
        context_text: str,
        chunk_seconds: float,
        timeout_sec: float,
        debug_hook=None,
    ) -> InsightModelResult:
        _ = (analysis_model, keywords, current_text, context_text, chunk_seconds, timeout_sec, debug_hook)
        raise AssertionError("analyze_text should not be called when STT fails")


class _FakeNotifier:
    def __init__(self) -> None:
        self.events = []

    def notify_event(self, event, **kwargs) -> bool:
        if not bool(getattr(event, "important", False)):
            return False
        self.events.append((event, dict(kwargs)))
        return True


class StageProcessorDualParamTests(unittest.TestCase):
    @staticmethod
    def _chunk(seq: int, *, status: str = "ok", text: str = "t") -> TranscriptChunk:
        return TranscriptChunk(
            chunk_seq=int(seq),
            chunk_file=f"asr_sentence_{int(seq):06d}.txt",
            ts_local="20260309_180000",
            text=f"{text}-{seq}",
            status=status,
            error="",
            attempt_count=1,
            elapsed_sec=0.0,
        )

    def test_stage_specific_retry_and_timeout_are_applied(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            chunk = base / "chunk_000001.mp3"
            chunk.write_bytes(b"audio")

            cfg = RealtimeInsightConfig(
                enabled=True,
                stt_request_timeout_sec=2.0,
                stt_stage_timeout_sec=5.0,
                stt_retry_count=2,
                stt_retry_interval_sec=0.05,
                analysis_request_timeout_sec=3.0,
                analysis_stage_timeout_sec=6.0,
                analysis_retry_count=2,
                analysis_retry_interval_sec=0.07,
                context_recent_required=0,
                context_wait_timeout_sec_1=0.0,
                context_wait_timeout_sec_2=0.0,
                context_wait_timeout_sec=0.0,
                context_target_chunks=18,
                context_check_interval_sec=0.01,
                use_dual_context_wait=True,
            )
            client = _RetryClient()
            processor = InsightStageProcessor(
                session_dir=base,
                config=cfg,
                keywords=KeywordConfig(),
                client=client,  # type: ignore[arg-type]
                log_fn=lambda _: None,
            )
            processor.process_chunk(1, chunk)

            transcript_payload = json.loads((base / "realtime_transcripts.jsonl").read_text(encoding="utf-8"))
            insight_payload = json.loads((base / "realtime_insights.jsonl").read_text(encoding="utf-8"))
            trace_rows = [
                json.loads(line)
                for line in (base / "analysis_prompt_trace.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

            self.assertEqual(transcript_payload["status"], "ok")
            self.assertEqual(transcript_payload["attempt_count"], 2)
            self.assertGreaterEqual(float(transcript_payload["elapsed_sec"]), 0.05)
            self.assertAlmostEqual(client.stt_timeouts[0], 2.0, places=3)

            self.assertEqual(insight_payload["status"], "ok")
            self.assertEqual(insight_payload["attempt_count"], 2)
            self.assertGreaterEqual(float(insight_payload["analysis_elapsed_sec"]), 0.05)
            self.assertAlmostEqual(client.analysis_timeouts[0], 3.0, places=3)
            self.assertEqual(insight_payload["context_reason"], "full18_ready")
            self.assertEqual(insight_payload["context_missing_ranges"], [])
            self.assertTrue(trace_rows)
            self.assertEqual(trace_rows[0]["attempt"], 2)
            self.assertEqual(trace_rows[0]["chunk_seconds"], 10.0)
            self.assertIn("历史上下文区", trace_rows[0]["context_text"])

    def test_startup_chunk_uses_ramped_recent_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            chunk = base / "chunk_000001.mp3"
            chunk.write_bytes(b"audio")

            cfg = RealtimeInsightConfig(
                enabled=True,
                stt_request_timeout_sec=2.0,
                stt_stage_timeout_sec=5.0,
                stt_retry_count=1,
                stt_retry_interval_sec=0.01,
                analysis_request_timeout_sec=3.0,
                analysis_stage_timeout_sec=6.0,
                analysis_retry_count=1,
                analysis_retry_interval_sec=0.01,
                context_recent_required=4,
                context_wait_timeout_sec_1=1.0,
                context_wait_timeout_sec_2=5.0,
                context_wait_timeout_sec=5.0,
                context_target_chunks=18,
                context_check_interval_sec=0.01,
                use_dual_context_wait=True,
            )
            processor = InsightStageProcessor(
                session_dir=base,
                config=cfg,
                keywords=KeywordConfig(),
                client=_AlwaysOkClient(),  # type: ignore[arg-type]
                log_fn=lambda _: None,
            )
            processor.process_chunk(1, chunk)

            insight_payload = json.loads((base / "realtime_insights.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(insight_payload["status"], "ok")
            self.assertEqual(insight_payload["context_reason"], "full18_ready")
            self.assertEqual(insight_payload["context_chunk_count"], 0)

    def test_history_read_uses_memory_window_without_disk_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = RealtimeInsightConfig(
                enabled=True,
                context_recent_required=1,
                context_target_chunks=2,
                context_min_ready=1,
            )
            processor = InsightStageProcessor(
                session_dir=base,
                config=cfg,
                keywords=KeywordConfig(),
                client=_AlwaysOkClient(),  # type: ignore[arg-type]
                log_fn=lambda _: None,
            )
            processor.append_transcript(self._chunk(1))
            processor.append_transcript(self._chunk(2))

            # Build history from memory window; any disk read would fail this test.
            with mock.patch.object(Path, "read_text", side_effect=AssertionError("disk read is not expected")):
                history = processor.load_history_chunks(3)

            self.assertEqual([item.chunk_seq for item in history], [1, 2])

    def test_history_window_incremental_visibility(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = RealtimeInsightConfig(
                enabled=True,
                context_recent_required=0,
                context_target_chunks=5,
                context_min_ready=0,
            )
            processor = InsightStageProcessor(
                session_dir=base,
                config=cfg,
                keywords=KeywordConfig(),
                client=_AlwaysOkClient(),  # type: ignore[arg-type]
                log_fn=lambda _: None,
            )
            processor.append_transcript(self._chunk(1))
            processor.append_transcript(self._chunk(2))
            processor.append_transcript(self._chunk(3, status="transcript_drop_error"))

            history = processor.load_history_chunks(4)
            self.assertEqual([item.chunk_seq for item in history], [1, 2])

    def test_history_window_eviction_keeps_recent_entries(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = RealtimeInsightConfig(
                enabled=True,
                context_recent_required=1,
                context_target_chunks=2,
                context_min_ready=1,
            )
            processor = InsightStageProcessor(
                session_dir=base,
                config=cfg,
                keywords=KeywordConfig(),
                client=_AlwaysOkClient(),  # type: ignore[arg-type]
                log_fn=lambda _: None,
            )

            for seq in range(1, 301):
                processor.append_transcript(self._chunk(seq))

            all_chunks = processor.load_transcript_chunks()
            self.assertEqual(len(all_chunks), 256)
            self.assertEqual(all_chunks[0].chunk_seq, 45)
            self.assertEqual(all_chunks[-1].chunk_seq, 300)

    def test_stage_processor_realtime_logs_rotate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = RealtimeInsightConfig(
                enabled=True,
                log_rotate_max_bytes=128,
                log_rotate_backup_count=2,
                context_recent_required=0,
                context_target_chunks=1,
                context_min_ready=0,
            )
            processor = InsightStageProcessor(
                session_dir=base,
                config=cfg,
                keywords=KeywordConfig(),
                client=_AlwaysOkClient(),  # type: ignore[arg-type]
                log_fn=lambda _: None,
            )
            for seq in range(1, 20):
                processor.append_transcript(self._chunk(seq, text="x" * 40))
                processor.append_analysis_prompt_trace({"seq": seq, "text": "y" * 60})

            self.assertTrue((base / "realtime_transcripts.jsonl.1").exists())
            self.assertTrue((base / "analysis_prompt_trace.jsonl.1").exists())

    def test_analysis_billing_alert_emits_system_event(self) -> None:
        reset_billing_alert_cooldown_for_tests()
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            notifier = _FakeNotifier()
            cfg = RealtimeInsightConfig(
                enabled=True,
                dingtalk_enabled=True,
                analysis_retry_count=1,
                analysis_retry_interval_sec=0.0,
                context_recent_required=0,
                context_wait_timeout_sec_1=0.0,
                context_wait_timeout_sec_2=0.0,
                context_wait_timeout_sec=0.0,
                context_target_chunks=1,
                use_dual_context_wait=True,
            )
            processor = InsightStageProcessor(
                session_dir=base,
                config=cfg,
                keywords=KeywordConfig(),
                client=_AnalyzeBillingClient(),  # type: ignore[arg-type]
                notifier=notifier,  # type: ignore[arg-type]
                log_fn=lambda _: None,
            )
            processor.process_transcript_event(
                chunk_seq=1,
                chunk_file="asr_sentence_000001.txt",
                transcript_text="课堂文本",
            )

            self.assertEqual(len(notifier.events), 1)
            event, _meta = notifier.events[0]
            self.assertEqual(getattr(event, "reason", ""), "billing_arrears_openai")
            self.assertEqual(getattr(event, "status", ""), "billing_alert")
            self.assertTrue(getattr(event, "important", False))
            details = list(getattr(event, "key_details", []))
            self.assertTrue(any("payment_url=https://platform.openai.com" in item for item in details))

    def test_stt_billing_alert_respects_service_cooldown(self) -> None:
        reset_billing_alert_cooldown_for_tests()
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            chunk1 = base / "chunk_000001.mp3"
            chunk2 = base / "chunk_000002.mp3"
            chunk1.write_bytes(b"audio")
            chunk2.write_bytes(b"audio")

            notifier = _FakeNotifier()
            cfg = RealtimeInsightConfig(
                enabled=True,
                dingtalk_enabled=True,
                stt_retry_count=1,
                stt_retry_interval_sec=0.0,
            )
            processor = InsightStageProcessor(
                session_dir=base,
                config=cfg,
                keywords=KeywordConfig(),
                client=_TranscribeBillingClient(),  # type: ignore[arg-type]
                notifier=notifier,  # type: ignore[arg-type]
                log_fn=lambda _: None,
            )
            processor.process_chunk(1, chunk1)
            processor.process_chunk(2, chunk2)

            self.assertEqual(len(notifier.events), 1)
            event, _meta = notifier.events[0]
            self.assertEqual(getattr(event, "reason", ""), "billing_arrears_openai")


if __name__ == "__main__":
    unittest.main()
