from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.live.insight.models import RealtimeInsightConfig
from src.simulator.mode1_validation import run_mode1_validation
from src.simulator.models import Mode1ValidationConfig


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    text = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    path.write_text(text + ("\n" if text else ""), encoding="utf-8")


class Mode1ValidationTests(unittest.TestCase):
    def test_validation_passes_for_legal_state_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_jsonl(
                base / "realtime_transcripts.jsonl",
                [
                    {
                        "chunk_seq": 1,
                        "chunk_file": "chunk_000001.mp3",
                        "ts_local": "20260101_000001",
                        "text": "hello",
                        "status": "ok",
                        "error": "",
                        "attempt_count": 1,
                        "elapsed_sec": 0.2,
                    },
                    {
                        "chunk_seq": 2,
                        "chunk_file": "chunk_000002.mp3",
                        "ts_local": "20260101_000002",
                        "text": "",
                        "status": "transcript_drop_timeout",
                        "error": "timeout",
                        "attempt_count": 4,
                        "elapsed_sec": 31.5,
                    },
                ],
            )
            _write_jsonl(
                base / "realtime_insights.jsonl",
                [
                    {
                        "chunk_seq": 1,
                        "chunk_file": "chunk_000001.mp3",
                        "status": "ok",
                        "attempt_count": 1,
                        "analysis_elapsed_sec": 1.2,
                        "context_chunk_count": 0,
                        "context_reason": "full18_ready",
                        "context_missing_ranges": [],
                    }
                ],
            )
            report = run_mode1_validation(
                run_session_dir=base,
                run_summary={"emitted_chunks": 2},
                config=RealtimeInsightConfig(),
            )
            self.assertTrue(report["passed"])
            self.assertEqual(report["failure_count"], 0)
            self.assertTrue((base / "mode1_validation_report.json").exists())

    def test_validation_fails_when_drop_chunk_has_insight(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_jsonl(
                base / "realtime_transcripts.jsonl",
                [
                    {
                        "chunk_seq": 1,
                        "chunk_file": "chunk_000001.mp3",
                        "ts_local": "20260101_000001",
                        "text": "",
                        "status": "transcript_drop_error",
                        "error": "x",
                        "attempt_count": 1,
                        "elapsed_sec": 0.1,
                    }
                ],
            )
            _write_jsonl(
                base / "realtime_insights.jsonl",
                [
                    {
                        "chunk_seq": 1,
                        "chunk_file": "chunk_000001.mp3",
                        "status": "ok",
                        "attempt_count": 1,
                        "analysis_elapsed_sec": 0.5,
                        "context_chunk_count": 0,
                        "context_reason": "full18_ready",
                        "context_missing_ranges": [],
                    }
                ],
            )
            report = run_mode1_validation(
                run_session_dir=base,
                run_summary={"emitted_chunks": 1},
                config=RealtimeInsightConfig(),
            )
            self.assertFalse(report["passed"])
            self.assertGreater(report["failure_count"], 0)

    def test_validation_coverage_missing_strict_fail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_jsonl(
                base / "realtime_transcripts.jsonl",
                [
                    {
                        "chunk_seq": 1,
                        "chunk_file": "chunk_000001.mp3",
                        "ts_local": "20260101_000001",
                        "text": "hello",
                        "status": "ok",
                        "error": "",
                        "attempt_count": 1,
                        "elapsed_sec": 0.1,
                    }
                ],
            )
            _write_jsonl(
                base / "realtime_insights.jsonl",
                [
                    {
                        "chunk_seq": 1,
                        "chunk_file": "chunk_000001.mp3",
                        "status": "ok",
                        "attempt_count": 1,
                        "analysis_elapsed_sec": 0.2,
                        "context_chunk_count": 0,
                        "context_reason": "full18_ready",
                        "context_missing_ranges": [],
                        "is_recovery": False,
                    }
                ],
            )
            report = run_mode1_validation(
                run_session_dir=base,
                run_summary={"emitted_chunks": 1, "emitted_seqs": [1]},
                config=RealtimeInsightConfig(),
                validation_config=Mode1ValidationConfig(
                    strict_fail=True,
                    required_branches=["analysis.drop_error"],
                ),
            )
            self.assertFalse(report["coverage_passed"])
            self.assertFalse(report["passed"])
            self.assertIn("analysis.drop_error", report["missing_branches"])

    def test_validation_coverage_missing_non_strict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            _write_jsonl(
                base / "realtime_transcripts.jsonl",
                [
                    {
                        "chunk_seq": 1,
                        "chunk_file": "chunk_000001.mp3",
                        "ts_local": "20260101_000001",
                        "text": "hello",
                        "status": "ok",
                        "error": "",
                        "attempt_count": 1,
                        "elapsed_sec": 0.1,
                    }
                ],
            )
            _write_jsonl(
                base / "realtime_insights.jsonl",
                [
                    {
                        "chunk_seq": 1,
                        "chunk_file": "chunk_000001.mp3",
                        "status": "ok",
                        "attempt_count": 1,
                        "analysis_elapsed_sec": 0.2,
                        "context_chunk_count": 0,
                        "context_reason": "full18_ready",
                        "context_missing_ranges": [],
                        "is_recovery": False,
                    }
                ],
            )
            report = run_mode1_validation(
                run_session_dir=base,
                run_summary={"emitted_chunks": 1, "emitted_seqs": [1]},
                config=RealtimeInsightConfig(),
                validation_config=Mode1ValidationConfig(
                    strict_fail=False,
                    required_branches=["analysis.drop_error"],
                ),
            )
            self.assertFalse(report["coverage_passed"])
            self.assertTrue(report["passed"])


if __name__ == "__main__":
    unittest.main()
