from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from src.cli.parser import build_parser
from src.live.insight.models import KeywordConfig, RealtimeInsightConfig
from src.live.insight.stage_processor import InsightStageProcessor
from src.simulator.cache_store import SimulationCacheStore
from src.simulator.mode_runner import run_mode
from src.simulator.models import (
    Mode6AnalysisStep,
    Mode6Case,
    Mode6CaseConfig,
    Mode6Config,
    Mode6Expected,
    Mode6HistoryItem,
    Mode6SttStep,
    Scenario,
    SimulatorMode,
)
from src.simulator.service import run_simulate


class Mode6RunnerTests(unittest.TestCase):
    def _base_processor(self, base: Path) -> InsightStageProcessor:
        config = RealtimeInsightConfig(
            enabled=True,
            request_timeout_sec=8.0,
            retry_count=4,
            stage_timeout_sec=32.0,
            context_recent_required=4,
            context_target_chunks=18,
            context_wait_timeout_sec=5.0,
            context_wait_timeout_sec_1=1.0,
            context_wait_timeout_sec_2=5.0,
            context_check_interval_sec=0.2,
            use_dual_context_wait=True,
            context_min_ready=0,
        )
        return InsightStageProcessor(
            session_dir=base,
            config=config,
            keywords=KeywordConfig(),
            client=None,
            log_fn=lambda _: None,
        )

    def test_mode6_run_mode_pass(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            case = Mode6Case(
                id="pass_case",
                chunk_seq=5,
                config=Mode6CaseConfig(),
                stt_script=[Mode6SttStep(type="ok", text="ok")],
                history_initial=[
                    Mode6HistoryItem(seq=1, text="h1"),
                    Mode6HistoryItem(seq=2, text="h2"),
                    Mode6HistoryItem(seq=3, text="h3"),
                    Mode6HistoryItem(seq=4, text="h4"),
                ],
                expected=Mode6Expected(
                    stt_status="ok",
                    stt_attempts=1,
                    analysis_called=True,
                    analysis_status="ok",
                    analysis_attempts=1,
                    context_reason="full18_ready",
                    context_chunk_count=4,
                    missing_ranges=[],
                ),
            )
            scenario = Scenario(
                mode=SimulatorMode.MODE6,
                name="m6-pass",
                mode6=Mode6Config(check_interval_sec=0.2, cases=[case]),
            )

            result = run_mode(
                mode=SimulatorMode.MODE6,
                scenario=scenario,
                chunk_paths=[],
                chunk_seconds=10,
                processor=self._base_processor(base),
                cache_store=SimulationCacheStore(base / "cache"),
                client=None,
                keywords=KeywordConfig(),
                stt_model="stt",
                analysis_model="ana",
                request_timeout_sec=8.0,
                precompute_workers=1,
                output_dir=base,
                log_fn=lambda _: None,
                seed_override=1,
            )
            self.assertEqual(result.summary["pass_count"], 1)
            self.assertEqual(result.summary["fail_count"], 0)
            self.assertTrue((base / "mode6_report.json").exists())
            self.assertTrue(Path(result.summary["trace_file"]).exists())

    def test_mode6_run_mode_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            case = Mode6Case(
                id="fail_case",
                chunk_seq=5,
                stt_script=[Mode6SttStep(type="ok", text="ok")],
                history_initial=[
                    Mode6HistoryItem(seq=1, text="h1"),
                    Mode6HistoryItem(seq=2, text="h2"),
                    Mode6HistoryItem(seq=3, text="h3"),
                    Mode6HistoryItem(seq=4, text="h4"),
                ],
                expected=Mode6Expected(
                    stt_status="ok",
                    stt_attempts=1,
                    analysis_called=True,
                    analysis_attempts=2,
                ),
            )
            scenario = Scenario(
                mode=SimulatorMode.MODE6,
                name="m6-fail",
                mode6=Mode6Config(check_interval_sec=0.2, cases=[case]),
            )
            result = run_mode(
                mode=SimulatorMode.MODE6,
                scenario=scenario,
                chunk_paths=[],
                chunk_seconds=10,
                processor=self._base_processor(base),
                cache_store=SimulationCacheStore(base / "cache"),
                client=None,
                keywords=KeywordConfig(),
                stt_model="stt",
                analysis_model="ana",
                request_timeout_sec=8.0,
                precompute_workers=1,
                output_dir=base,
                log_fn=lambda _: None,
                seed_override=1,
            )
            self.assertEqual(result.summary["case_count"], 1)
            self.assertEqual(result.summary["fail_count"], 1)
            self.assertFalse(result.summary["cases"][0]["passed"])
            self.assertIn("analysis_attempts", ";".join(result.summary["cases"][0]["failures"]))

    def test_mode6_analysis_timeout_then_success_with_elapsed_limit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            history = [Mode6HistoryItem(seq=idx, text=f"h{idx}") for idx in range(1, 19)]
            case = Mode6Case(
                id="analysis_timeout_then_ok",
                chunk_seq=19,
                config=Mode6CaseConfig(
                    request_timeout_sec=15.0,
                    stage_timeout_sec=60.0,
                    retry_count=4,
                ),
                stt_script=[Mode6SttStep(type="ok", text="ok")],
                analysis_script=[
                    Mode6AnalysisStep(type="timeout_request", error="first timeout"),
                    Mode6AnalysisStep(type="ok"),
                ],
                history_initial=history,
                expected=Mode6Expected(
                    stt_status="ok",
                    stt_attempts=1,
                    analysis_called=True,
                    analysis_status="ok",
                    analysis_attempts=2,
                    analysis_elapsed_sec_lte=20.0,
                    context_reason="full18_ready",
                    context_chunk_count=18,
                ),
            )
            scenario = Scenario(
                mode=SimulatorMode.MODE6,
                name="m6-analysis-timeout-ok",
                mode6=Mode6Config(check_interval_sec=0.2, cases=[case]),
            )
            result = run_mode(
                mode=SimulatorMode.MODE6,
                scenario=scenario,
                chunk_paths=[],
                chunk_seconds=10,
                processor=self._base_processor(base),
                cache_store=SimulationCacheStore(base / "cache"),
                client=None,
                keywords=KeywordConfig(),
                stt_model="stt",
                analysis_model="ana",
                request_timeout_sec=8.0,
                precompute_workers=1,
                output_dir=base,
                log_fn=lambda _: None,
                seed_override=1,
            )
            self.assertEqual(result.summary["pass_count"], 1)

    def test_run_simulate_mode6_is_offline_and_strict(self) -> None:
        parser = build_parser()
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            sim_root = base / "sim"
            sim_root.mkdir(parents=True, exist_ok=True)
            run_dir = base / "runs"
            run_dir.mkdir(parents=True, exist_ok=True)
            scenario_path = base / "m6.yaml"
            scenario_path.write_text(
                textwrap.dedent(
                    """
                    mode: 6
                    name: m6_cli
                    mode6:
                      cases:
                        - id: c1
                          chunk_seq: 5
                          stt_script:
                            - type: ok
                              text: hello
                          history:
                            initial:
                              - seq: 1
                                text: h1
                              - seq: 2
                                text: h2
                              - seq: 3
                                text: h3
                              - seq: 4
                                text: h4
                          expected:
                            stt_status: ok
                            stt_attempts: 1
                            analysis_called: true
                            context_reason: full18_ready
                            context_chunk_count: 4
                    """
                ).strip(),
                encoding="utf-8",
            )

            args = parser.parse_args(
                [
                    "simulate",
                    "--mode",
                    "6",
                    "--scenario-file",
                    str(scenario_path),
                    "--sim-root",
                    str(sim_root),
                    "--run-dir",
                    str(run_dir),
                ]
            )
            code = run_simulate(args)
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
