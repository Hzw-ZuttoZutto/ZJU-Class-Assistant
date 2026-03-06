from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.simulator.mode2_validation import run_mode2_validation
from src.simulator.models import (
    Mode2ValidationConfig,
    Mode2ValidationPrecompute,
    Mode2ValidationRun,
    Mode2ValidationSeq,
    Scenario,
    SimulatorMode,
)


class Mode2ValidationTests(unittest.TestCase):
    def test_mode2_validation_pass(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "realtime_transcripts.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"chunk_seq": 1, "status": "ok", "text": "t1"}, ensure_ascii=False),
                        json.dumps({"chunk_seq": 2, "status": "transcript_drop_timeout", "text": ""}, ensure_ascii=False),
                        json.dumps({"chunk_seq": 4, "status": "ok", "text": "forced"}, ensure_ascii=False),
                    ]
                ),
                encoding="utf-8",
            )
            (base / "realtime_insights.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"chunk_seq": 1, "status": "ok"}, ensure_ascii=False),
                        json.dumps({"chunk_seq": 4, "status": "ok"}, ensure_ascii=False),
                    ]
                ),
                encoding="utf-8",
            )
            scenario = Scenario(
                mode=SimulatorMode.MODE2,
                name="m2",
                mode2_validation=Mode2ValidationConfig(
                    strict_fail=True,
                    precompute=Mode2ValidationPrecompute(
                        stt_failures=0,
                        analysis_failures=0,
                    ),
                    run=Mode2ValidationRun(
                        emitted_chunks=4,
                        translation_rules_applied=2,
                        analysis_rules_applied=0,
                    ),
                    seq=[
                        Mode2ValidationSeq(
                            seq=2,
                            transcript_status="transcript_drop_timeout",
                            insight_present=False,
                        ),
                        Mode2ValidationSeq(
                            seq=4,
                            transcript_status="ok",
                            insight_present=True,
                            forced_text_exact="forced",
                        ),
                    ],
                ),
            )
            report = run_mode2_validation(
                scenario=scenario,
                run_summary={
                    "emitted_chunks": 4,
                    "translation_rules_applied": 2,
                    "analysis_rules_applied": 0,
                    "trace_file": "/tmp/mode2_trace.jsonl",
                },
                precompute_manifest={
                    "stt": {"failures": 0, "misses": 4, "computed": 4},
                    "analysis": {"failures": 0, "misses": 4, "computed": 4},
                },
                run_session_dir=base,
            )
            self.assertTrue(report["passed"])
            self.assertEqual(report["failure_count"], 0)
            self.assertTrue((base / "validation_report.json").exists())

    def test_mode2_validation_failure_reports_diff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "realtime_transcripts.jsonl").write_text(
                json.dumps({"chunk_seq": 2, "status": "ok", "text": "x"}, ensure_ascii=False),
                encoding="utf-8",
            )
            scenario = Scenario(
                mode=SimulatorMode.MODE2,
                name="m2",
                mode2_validation=Mode2ValidationConfig(
                    strict_fail=True,
                    run=Mode2ValidationRun(emitted_chunks=3),
                    seq=[Mode2ValidationSeq(seq=2, transcript_status="transcript_drop_timeout")],
                ),
            )
            report = run_mode2_validation(
                scenario=scenario,
                run_summary={"emitted_chunks": 1},
                precompute_manifest=None,
                run_session_dir=base,
            )
            self.assertFalse(report["passed"])
            self.assertGreater(report["failure_count"], 0)
            self.assertTrue(any("run.emitted_chunks" in item for item in report["failures"]))
            self.assertTrue(any("seq[2].transcript_status" in item for item in report["failures"]))

    def test_mode3_validation_pass_with_mask_and_forced_result(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            (base / "realtime_transcripts.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"chunk_seq": 3, "status": "ok", "text": "t3"}, ensure_ascii=False),
                        json.dumps({"chunk_seq": 4, "status": "ok", "text": "t4"}, ensure_ascii=False),
                        json.dumps({"chunk_seq": 5, "status": "ok", "text": "t5"}, ensure_ascii=False),
                    ]
                ),
                encoding="utf-8",
            )
            (base / "realtime_insights.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {"chunk_seq": 3, "status": "analysis_drop_timeout", "context_chunk_count": 2},
                            ensure_ascii=False,
                        ),
                        json.dumps({"chunk_seq": 4, "status": "ok", "context_chunk_count": 1}, ensure_ascii=False),
                        json.dumps({"chunk_seq": 5, "status": "ok", "context_chunk_count": 2}, ensure_ascii=False),
                    ]
                ),
                encoding="utf-8",
            )
            (base / "mode3_trace.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "out_seq": 3,
                                "history_visibility_mask": "",
                                "forced_result_applied": False,
                                "forced_text_applied": False,
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "out_seq": 4,
                                "history_visibility_mask": "111111110011001100",
                                "forced_result_applied": False,
                                "forced_text_applied": False,
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(
                            {
                                "out_seq": 5,
                                "history_visibility_mask": "111111110011001100",
                                "forced_result_applied": True,
                                "forced_text_applied": False,
                            },
                            ensure_ascii=False,
                        ),
                    ]
                ),
                encoding="utf-8",
            )
            scenario = Scenario(
                mode=SimulatorMode.MODE3,
                name="m3",
                mode2_validation=Mode2ValidationConfig(
                    strict_fail=True,
                    run=Mode2ValidationRun(mode3_variant="controlled_history"),
                    seq=[
                        Mode2ValidationSeq(
                            seq=3,
                            transcript_status="ok",
                            insight_status="analysis_drop_timeout",
                            context_chunk_count=2,
                            history_visibility_mask="",
                            forced_result_applied=False,
                        ),
                        Mode2ValidationSeq(
                            seq=5,
                            transcript_status="ok",
                            insight_status="ok",
                            context_chunk_count=2,
                            history_visibility_mask="111111110011001100",
                            forced_result_applied=True,
                        ),
                    ],
                ),
            )
            report = run_mode2_validation(
                scenario=scenario,
                run_summary={
                    "mode3_variant": "controlled_history",
                    "trace_file": str(base / "mode3_trace.jsonl"),
                },
                precompute_manifest=None,
                run_session_dir=base,
            )
            self.assertTrue(report["passed"])
            self.assertEqual(report["failure_count"], 0)


if __name__ == "__main__":
    unittest.main()
