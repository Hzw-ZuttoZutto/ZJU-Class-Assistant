from __future__ import annotations

import unittest

from src.cli.parser import build_parser


class SimulatorCliTests(unittest.TestCase):
    def test_simulate_args_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "simulate",
                "--mode",
                "2",
                "--scenario-file",
                "tests/simulator/scenarios/mode2/sample.yaml",
            ]
        )
        self.assertEqual(args.mode, 2)
        self.assertEqual(args.sim_root, "tests/simulator")
        self.assertEqual(args.mp3_dir, "tests/simulator/mp3_inputs")
        self.assertEqual(args.run_dir, "tests/simulator/runs")
        self.assertEqual(args.chunk_seconds, 10)
        self.assertEqual(args.precompute_workers, 4)
        self.assertEqual(args.rt_model, "gpt-4.1-mini")
        self.assertEqual(args.rt_stt_model, "whisper-large-v3")
        self.assertEqual(args.rt_keywords_file, "config/realtime_keywords.json")
        self.assertEqual(args.rt_api_base_url, "")
        self.assertEqual(args.rt_stt_request_timeout_sec, 8.0)
        self.assertEqual(args.rt_stt_stage_timeout_sec, 32.0)
        self.assertEqual(args.rt_stt_retry_count, 4)
        self.assertEqual(args.rt_stt_retry_interval_sec, 0.2)
        self.assertEqual(args.rt_analysis_request_timeout_sec, 15.0)
        self.assertEqual(args.rt_analysis_stage_timeout_sec, 60.0)
        self.assertEqual(args.rt_analysis_retry_count, 4)
        self.assertEqual(args.rt_analysis_retry_interval_sec, 0.2)
        self.assertEqual(args.rt_context_recent_required, 4)
        self.assertEqual(args.rt_context_wait_timeout_sec_1, 1.0)
        self.assertEqual(args.rt_context_wait_timeout_sec_2, 5.0)
        self.assertIsNone(args.seed)
        self.assertEqual(args.mode5_profile, "all_chunks_dual")
        self.assertIsNone(args.mode5_target_seq)

    def test_simulate_args_custom(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "simulate",
                "--mode",
                "5",
                "--scenario-file",
                "/tmp/s.yaml",
                "--sim-root",
                "/tmp/sim",
                "--mp3-dir",
                "/tmp/mp3",
                "--run-dir",
                "/tmp/run",
                "--chunk-seconds",
                "12",
                "--precompute-workers",
                "6",
                "--rt-model",
                "gpt-5-mini",
                "--rt-stt-model",
                "whisper-large-v3",
                "--rt-keywords-file",
                "/tmp/k.json",
                "--rt-api-base-url",
                "https://aihubmix.com/v1",
                "--rt-stt-request-timeout-sec",
                "8",
                "--rt-stt-stage-timeout-sec",
                "40",
                "--rt-stt-retry-count",
                "1",
                "--rt-stt-retry-interval-sec",
                "0.3",
                "--rt-analysis-request-timeout-sec",
                "12",
                "--rt-analysis-stage-timeout-sec",
                "55",
                "--rt-analysis-retry-count",
                "2",
                "--rt-analysis-retry-interval-sec",
                "0.4",
                "--rt-context-recent-required",
                "3",
                "--rt-context-wait-timeout-sec-1",
                "2",
                "--rt-context-wait-timeout-sec-2",
                "6",
                "--seed",
                "123",
                "--mode5-profile",
                "single_chunk_dual",
                "--mode5-target-seq",
                "4",
            ]
        )
        self.assertEqual(args.mode, 5)
        self.assertEqual(args.sim_root, "/tmp/sim")
        self.assertEqual(args.mp3_dir, "/tmp/mp3")
        self.assertEqual(args.run_dir, "/tmp/run")
        self.assertEqual(args.chunk_seconds, 12)
        self.assertEqual(args.precompute_workers, 6)
        self.assertEqual(args.rt_keywords_file, "/tmp/k.json")
        self.assertEqual(args.rt_api_base_url, "https://aihubmix.com/v1")
        self.assertEqual(args.rt_stt_request_timeout_sec, 8.0)
        self.assertEqual(args.rt_stt_stage_timeout_sec, 40.0)
        self.assertEqual(args.rt_stt_retry_count, 1)
        self.assertEqual(args.rt_stt_retry_interval_sec, 0.3)
        self.assertEqual(args.rt_analysis_request_timeout_sec, 12.0)
        self.assertEqual(args.rt_analysis_stage_timeout_sec, 55.0)
        self.assertEqual(args.rt_analysis_retry_count, 2)
        self.assertEqual(args.rt_analysis_retry_interval_sec, 0.4)
        self.assertEqual(args.rt_context_recent_required, 3)
        self.assertEqual(args.rt_context_wait_timeout_sec_1, 2.0)
        self.assertEqual(args.rt_context_wait_timeout_sec_2, 6.0)
        self.assertEqual(args.seed, 123)
        self.assertEqual(args.mode5_profile, "single_chunk_dual")
        self.assertEqual(args.mode5_target_seq, 4)

    def test_simulate_mode6_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "simulate",
                "--mode",
                "6",
                "--scenario-file",
                "tests/simulator/scenarios/mode6/example.yaml",
            ]
        )
        self.assertEqual(args.mode, 6)
        self.assertEqual(args.scenario_file, "tests/simulator/scenarios/mode6/example.yaml")


if __name__ == "__main__":
    unittest.main()
