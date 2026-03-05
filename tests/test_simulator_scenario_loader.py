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


if __name__ == "__main__":
    unittest.main()
