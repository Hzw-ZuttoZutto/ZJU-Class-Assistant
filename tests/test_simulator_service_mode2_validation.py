from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from src.cli.parser import build_parser
from src.simulator.mode_runner import ModeRunResult
from src.simulator.service import run_simulate


class SimulatorServiceMode2ValidationTests(unittest.TestCase):
    def test_mode2_strict_validation_failure_returns_nonzero(self) -> None:
        parser = build_parser()
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            sim_root = base / "sim"
            run_dir = base / "runs"
            sim_root.mkdir(parents=True, exist_ok=True)
            run_dir.mkdir(parents=True, exist_ok=True)

            scenario_path = base / "mode2_validation.yaml"
            scenario_path.write_text(
                textwrap.dedent(
                    """
                    mode: 2
                    name: mode2_validation_fail
                    validation:
                      strict_fail: true
                      run:
                        emitted_chunks: 2
                    """
                ).strip(),
                encoding="utf-8",
            )
            chunk_path = base / "chunk_000001.mp3"
            chunk_path.write_bytes(b"audio")
            args = parser.parse_args(
                [
                    "simulate",
                    "--mode",
                    "2",
                    "--scenario-file",
                    str(scenario_path),
                    "--sim-root",
                    str(sim_root),
                    "--run-dir",
                    str(run_dir),
                ]
            )
            with (
                patch("src.simulator.service.collect_input_mp3_files", return_value=[chunk_path]),
                patch("src.simulator.service.preprocess_mp3_to_chunks", return_value=[chunk_path]),
                patch("src.simulator.service._build_openai_client", return_value=object()),
                patch(
                    "src.simulator.service.run_precompute",
                    return_value={"stt": {"failures": 0}, "analysis": {"failures": 0}},
                ),
                patch("src.simulator.service.write_precompute_manifest", return_value=None),
                patch(
                    "src.simulator.service.run_mode",
                    return_value=ModeRunResult(
                        mode=2,
                        output_dir=run_dir,
                        summary={"mode": 2, "emitted_chunks": 1, "trace_file": "/tmp/mode2_trace.jsonl"},
                    ),
                ),
                patch(
                    "src.simulator.service.run_mode2_validation",
                    return_value={
                        "strict_fail": True,
                        "passed": False,
                        "failure_count": 1,
                        "failures": ["run.emitted_chunks expected=2 actual=1"],
                        "report_file": "/tmp/validation_report.json",
                    },
                ),
            ):
                code = run_simulate(args)
            self.assertEqual(code, 1)

    def test_mode3_strict_validation_failure_returns_nonzero(self) -> None:
        parser = build_parser()
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            sim_root = base / "sim"
            run_dir = base / "runs"
            sim_root.mkdir(parents=True, exist_ok=True)
            run_dir.mkdir(parents=True, exist_ok=True)

            scenario_path = base / "mode3_validation.yaml"
            scenario_path.write_text(
                textwrap.dedent(
                    """
                    mode: 3
                    name: mode3_validation_fail
                    mode3_variant: controlled_history
                    validation:
                      strict_fail: true
                      run:
                        emitted_chunks: 2
                    """
                ).strip(),
                encoding="utf-8",
            )
            chunk_path = base / "chunk_000001.mp3"
            chunk_path.write_bytes(b"audio")
            args = parser.parse_args(
                [
                    "simulate",
                    "--mode",
                    "3",
                    "--scenario-file",
                    str(scenario_path),
                    "--sim-root",
                    str(sim_root),
                    "--run-dir",
                    str(run_dir),
                ]
            )
            with (
                patch("src.simulator.service.collect_input_mp3_files", return_value=[chunk_path]),
                patch("src.simulator.service.preprocess_mp3_to_chunks", return_value=[chunk_path]),
                patch("src.simulator.service._build_openai_client", return_value=object()),
                patch(
                    "src.simulator.service.run_precompute",
                    return_value={"stt": {"failures": 0}, "analysis": {"failures": 0}},
                ),
                patch("src.simulator.service.write_precompute_manifest", return_value=None),
                patch(
                    "src.simulator.service.run_mode",
                    return_value=ModeRunResult(
                        mode=3,
                        output_dir=run_dir,
                        summary={
                            "mode": 3,
                            "emitted_chunks": 1,
                            "mode3_variant": "controlled_history",
                            "trace_file": "/tmp/mode3_trace.jsonl",
                        },
                    ),
                ),
                patch(
                    "src.simulator.service.run_mode2_validation",
                    return_value={
                        "strict_fail": True,
                        "passed": False,
                        "failure_count": 1,
                        "failures": ["run.emitted_chunks expected=2 actual=1"],
                        "report_file": "/tmp/validation_report.json",
                    },
                ),
            ):
                code = run_simulate(args)
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
