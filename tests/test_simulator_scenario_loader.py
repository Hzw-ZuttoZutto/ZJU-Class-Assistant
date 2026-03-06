from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from src.simulator.models import SimulatorMode
from src.simulator.scenario_loader import load_scenario, validate_visibility_mask


class ScenarioLoaderTests(unittest.TestCase):
    def test_load_valid_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "s.yaml"
            path.write_text(
                textwrap.dedent(
                    """
                    mode: 3
                    name: case-a
                    seed: 7
                    dataset:
                      files: [a.mp3, b.mp3]
                    feed:
                      mode: jitter
                      jitter:
                        max_sec: 1.5
                      drop: [2]
                      duplicate:
                        - seq: 1
                          times: 2
                      barriers:
                        - after_seq: 1
                          pause_sec: 3
                    control:
                      translation:
                        rules:
                          - seq: 1
                            status: timeout
                            delay_sec: 0.5
                      analysis:
                        rules:
                          - seq: 2
                            status: error
                    history:
                      by_seq:
                        - seq: 5
                          visibility: "111111110011001100"
                          hold_sec: 12
                    precompute:
                      workers: 8
                    benchmark:
                      parallel_workers: 6
                      repeats: 3
                    mode3_variant: controlled_history
                    """
                ).strip(),
                encoding="utf-8",
            )

            scenario = load_scenario(path, expected_mode=SimulatorMode.MODE3)
            self.assertEqual(scenario.mode, SimulatorMode.MODE3)
            self.assertEqual(scenario.name, "case-a")
            self.assertEqual(scenario.seed, 7)
            self.assertEqual(scenario.feed.mode, "jitter")
            self.assertEqual(scenario.feed.jitter_max_sec, 1.5)
            self.assertEqual(scenario.feed.drop, [2])
            self.assertEqual(len(scenario.translation_rules), 1)
            self.assertEqual(len(scenario.analysis_rules), 1)
            self.assertEqual(scenario.history_rules[0].visibility, "111111110011001100")
            self.assertEqual(scenario.precompute.workers, 8)
            self.assertEqual(scenario.benchmark.parallel_workers, 6)
            self.assertEqual(scenario.benchmark.repeats, 3)
            self.assertEqual(scenario.mode3_variant, "controlled_history")

    def test_reject_invalid_visibility_length(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.yaml"
            path.write_text(
                textwrap.dedent(
                    """
                    mode: 3
                    history:
                      by_seq:
                        - seq: 1
                          visibility: "111"
                    """
                ).strip(),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                _ = load_scenario(path)

    def test_reject_mode_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m.yaml"
            path.write_text("mode: 2\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                _ = load_scenario(path, expected_mode=SimulatorMode.MODE3)

    def test_validate_visibility_mask(self) -> None:
        validate_visibility_mask("111111110011001100")
        with self.assertRaises(ValueError):
            validate_visibility_mask("11111111001100110x")

    def test_load_mode6_case_driven_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m6.yaml"
            path.write_text(
                textwrap.dedent(
                    """
                    mode: 6
                    name: mode6-case
                    mode6:
                      check_interval_sec: 0.2
                      cases:
                        - id: c1
                          chunk_seq: 19
                          config:
                            request_timeout_sec: 8
                            stage_timeout_sec: 32
                            retry_count: 4
                            context_recent_required: 4
                            context_target_chunks: 18
                            context_wait_timeout_sec_1: 1
                            context_wait_timeout_sec_2: 5
                          stt_script:
                            - type: timeout_request
                            - type: ok
                              text: done
                          analysis_script:
                            - type: timeout_request
                            - type: ok
                              result:
                                important: true
                                summary: s1
                          history:
                            initial:
                              - seq: 15
                                text: h15
                              - seq: 16
                                text: h16
                              - seq: 17
                                text: h17
                              - seq: 18
                                text: h18
                            arrivals:
                              - at_sec: 0.6
                                seq: 1
                                text: h1
                          expected:
                            stt_status: ok
                            stt_attempts: 2
                            analysis_called: true
                            analysis_status: ok
                            analysis_attempts: 2
                            analysis_elapsed_sec_lte: 20
                            context_reason: full18_ready
                            context_chunk_count: 5
                            missing_ranges: []
                    """
                ).strip(),
                encoding="utf-8",
            )
            scenario = load_scenario(path, expected_mode=SimulatorMode.MODE6)
            self.assertEqual(int(scenario.mode), 6)
            self.assertAlmostEqual(scenario.mode6.check_interval_sec, 0.2)
            self.assertEqual(len(scenario.mode6.cases), 1)
            case = scenario.mode6.cases[0]
            self.assertEqual(case.id, "c1")
            self.assertEqual(case.chunk_seq, 19)
            self.assertEqual(case.stt_script[0].normalized_type(), "timeout_request")
            self.assertEqual(case.stt_script[1].text, "done")
            self.assertEqual(case.analysis_script[0].normalized_type(), "timeout_request")
            self.assertEqual(case.analysis_script[1].result["summary"], "s1")
            self.assertEqual(case.expected.analysis_status, "ok")
            self.assertEqual(case.expected.analysis_attempts, 2)
            self.assertEqual(case.expected.analysis_elapsed_sec_lte, 20.0)
            self.assertEqual(case.expected.context_reason, "full18_ready")

    def test_reject_mode6_duplicate_history_seq(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m6_bad.yaml"
            path.write_text(
                textwrap.dedent(
                    """
                    mode: 6
                    mode6:
                      cases:
                        - id: bad
                          chunk_seq: 10
                          stt_script:
                            - type: ok
                              text: hi
                          history:
                            initial:
                              - seq: 1
                                text: a
                            arrivals:
                              - at_sec: 0.2
                                seq: 1
                                text: b
                    """
                ).strip(),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                _ = load_scenario(path, expected_mode=SimulatorMode.MODE6)

    def test_load_mode2_validation_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m2_validation.yaml"
            path.write_text(
                textwrap.dedent(
                    """
                    mode: 2
                    name: mode2-validation
                    validation:
                      strict_fail: true
                      precompute:
                        stt_failures: 0
                        analysis_failures: 0
                      run:
                        emitted_chunks: 12
                        translation_rules_applied: 2
                      seq:
                        - seq: 2
                          transcript_status: transcript_drop_timeout
                          insight_present: false
                        - seq: 4
                          forced_text_exact: "abc"
                    """
                ).strip(),
                encoding="utf-8",
            )
            scenario = load_scenario(path, expected_mode=SimulatorMode.MODE2)
            self.assertTrue(scenario.mode2_validation.strict_fail)
            self.assertEqual(scenario.mode2_validation.precompute.stt_failures, 0)
            self.assertEqual(scenario.mode2_validation.precompute.analysis_failures, 0)
            self.assertEqual(scenario.mode2_validation.run.emitted_chunks, 12)
            self.assertEqual(scenario.mode2_validation.run.translation_rules_applied, 2)
            self.assertEqual(len(scenario.mode2_validation.seq), 2)
            self.assertEqual(scenario.mode2_validation.seq[0].seq, 2)
            self.assertEqual(scenario.mode2_validation.seq[0].transcript_status, "transcript_drop_timeout")
            self.assertEqual(scenario.mode2_validation.seq[0].insight_present, False)

    def test_reject_mode2_validation_negative_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m2_bad_validation.yaml"
            path.write_text(
                textwrap.dedent(
                    """
                    mode: 2
                    validation:
                      precompute:
                        stt_failures: -1
                    """
                ).strip(),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                _ = load_scenario(path, expected_mode=SimulatorMode.MODE2)

    def test_reject_mode2_validation_seq_without_valid_seq(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m2_bad_seq.yaml"
            path.write_text(
                textwrap.dedent(
                    """
                    mode: 2
                    validation:
                      seq:
                        - seq: 0
                          transcript_status: ok
                    """
                ).strip(),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                _ = load_scenario(path, expected_mode=SimulatorMode.MODE2)

    def test_load_mode3_validation_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m3_validation.yaml"
            path.write_text(
                textwrap.dedent(
                    """
                    mode: 3
                    mode3_variant: controlled_history
                    validation:
                      strict_fail: true
                      run:
                        mode3_variant: controlled_history
                      seq:
                        - seq: 5
                          transcript_status: ok
                          insight_status: ok
                          context_chunk_count: 2
                          history_visibility_mask: "111111110011001100"
                          forced_result_applied: true
                    """
                ).strip(),
                encoding="utf-8",
            )
            scenario = load_scenario(path, expected_mode=SimulatorMode.MODE3)
            self.assertTrue(scenario.mode2_validation.strict_fail)
            self.assertEqual(scenario.mode2_validation.run.mode3_variant, "controlled_history")
            self.assertEqual(len(scenario.mode2_validation.seq), 1)
            self.assertEqual(scenario.mode2_validation.seq[0].seq, 5)
            self.assertEqual(scenario.mode2_validation.seq[0].insight_status, "ok")
            self.assertEqual(scenario.mode2_validation.seq[0].context_chunk_count, 2)
            self.assertEqual(
                scenario.mode2_validation.seq[0].history_visibility_mask,
                "111111110011001100",
            )
            self.assertTrue(scenario.mode2_validation.seq[0].forced_result_applied)

    def test_reject_mode3_validation_invalid_visibility_mask(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m3_bad_validation.yaml"
            path.write_text(
                textwrap.dedent(
                    """
                    mode: 3
                    validation:
                      seq:
                        - seq: 4
                          history_visibility_mask: "101"
                    """
                ).strip(),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                _ = load_scenario(path, expected_mode=SimulatorMode.MODE3)

    def test_reject_mode6_invalid_analysis_script_type(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m6_bad_type.yaml"
            path.write_text(
                textwrap.dedent(
                    """
                    mode: 6
                    mode6:
                      cases:
                        - id: bad
                          chunk_seq: 10
                          stt_script:
                            - type: ok
                              text: hi
                          analysis_script:
                            - type: broken
                    """
                ).strip(),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                _ = load_scenario(path, expected_mode=SimulatorMode.MODE6)

    def test_reject_mode6_negative_analysis_delay(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m6_bad_delay.yaml"
            path.write_text(
                textwrap.dedent(
                    """
                    mode: 6
                    mode6:
                      cases:
                        - id: bad
                          chunk_seq: 10
                          stt_script:
                            - type: ok
                              text: hi
                          analysis_script:
                            - type: ok
                              delay_sec: -0.1
                    """
                ).strip(),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                _ = load_scenario(path, expected_mode=SimulatorMode.MODE6)

    def test_reject_mode6_invalid_expected_analysis_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m6_bad_expected.yaml"
            path.write_text(
                textwrap.dedent(
                    """
                    mode: 6
                    mode6:
                      cases:
                        - id: bad
                          chunk_seq: 10
                          stt_script:
                            - type: ok
                              text: hi
                          expected:
                            analysis_status: unknown
                    """
                ).strip(),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                _ = load_scenario(path, expected_mode=SimulatorMode.MODE6)


if __name__ == "__main__":
    unittest.main()
