from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.live.insight.models import KeywordConfig, RealtimeInsightConfig
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
        timeout_sec: float,
    ) -> InsightModelResult:
        self.analysis_calls += 1
        self.analysis_timeouts.append(float(timeout_sec))
        if self.analysis_calls == 1:
            raise RuntimeError("analysis fail once")
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
        timeout_sec: float,
    ) -> InsightModelResult:
        return InsightModelResult(
            important=False,
            summary="ok",
            context_summary="ok",
            matched_terms=[],
            reason="ok",
        )


class StageProcessorDualParamTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
