from __future__ import annotations

import unittest

from src.cli.parser import build_parser
from src.simulator.service import _build_runtime_config


class CliParserTests(unittest.TestCase):
    def test_scan_live_args_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "scan",
                "--username",
                "u",
                "--password",
                "p",
                "--teacher",
                "t",
                "--title",
                "c",
            ]
        )
        self.assertFalse(args.require_live)
        self.assertEqual(args.live_check_timeout, 30.0)
        self.assertEqual(args.live_check_interval, 2.0)

    def test_scan_live_args_custom(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "scan",
                "--username",
                "u",
                "--password",
                "p",
                "--teacher",
                "t",
                "--title",
                "c",
                "--require-live",
                "--live-check-timeout",
                "8",
                "--live-check-interval",
                "1.5",
            ]
        )
        self.assertTrue(args.require_live)
        self.assertEqual(args.live_check_timeout, 8.0)
        self.assertEqual(args.live_check_interval, 1.5)

    def test_watch_record_args_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "watch",
                "--course-id",
                "1",
                "--sub-id",
                "2",
            ]
        )
        self.assertEqual(args.record_dir, "")
        self.assertEqual(args.record_segment_minutes, 10)
        self.assertEqual(args.record_startup_av_timeout, 15.0)
        self.assertEqual(args.record_recovery_window_sec, 10.0)
        self.assertEqual(args.username, "")
        self.assertEqual(args.password, "")
        self.assertFalse(args.rt_insight_enabled)
        self.assertEqual(args.rt_chunk_seconds, 10)
        self.assertEqual(args.rt_context_window_seconds, 180)
        self.assertEqual(args.rt_model, "gpt-5-mini")
        self.assertEqual(args.rt_stt_model, "gpt-4o-mini-transcribe")
        self.assertEqual(args.rt_keywords_file, "config/realtime_keywords.json")
        self.assertEqual(args.rt_api_base_url, "")
        self.assertEqual(args.rt_request_timeout_sec, 12.0)
        self.assertEqual(args.rt_retry_count, 2)
        self.assertEqual(args.rt_alert_threshold, 90)
        self.assertEqual(args.rt_max_concurrency, 5)
        self.assertEqual(args.rt_stage_timeout_sec, 60.0)
        self.assertEqual(args.rt_context_min_ready, 15)
        self.assertEqual(args.rt_context_recent_required, 4)
        self.assertEqual(args.rt_context_wait_timeout_sec, 15.0)

    def test_watch_record_args_custom(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "watch",
                "--username",
                "u",
                "--password",
                "p",
                "--course-id",
                "1",
                "--sub-id",
                "2",
                "--record-dir",
                "/tmp/r",
                "--record-segment-minutes",
                "0",
                "--record-startup-av-timeout",
                "20",
                "--record-recovery-window-sec",
                "7",
                "--rt-insight-enabled",
                "--rt-chunk-seconds",
                "12",
                "--rt-context-window-seconds",
                "240",
                "--rt-model",
                "gpt-5-mini",
                "--rt-stt-model",
                "gpt-4o-mini-transcribe",
                "--rt-keywords-file",
                "/tmp/k.json",
                "--rt-api-base-url",
                "https://aihubmix.com/v1",
                "--rt-request-timeout-sec",
                "8",
                "--rt-retry-count",
                "4",
                "--rt-alert-threshold",
                "88",
                "--rt-max-concurrency",
                "3",
                "--rt-stage-timeout-sec",
                "45",
                "--rt-context-min-ready",
                "10",
                "--rt-context-recent-required",
                "3",
                "--rt-context-wait-timeout-sec",
                "9",
            ]
        )
        self.assertEqual(args.record_dir, "/tmp/r")
        self.assertEqual(args.record_segment_minutes, 0)
        self.assertEqual(args.record_startup_av_timeout, 20.0)
        self.assertEqual(args.record_recovery_window_sec, 7.0)
        self.assertTrue(args.rt_insight_enabled)
        self.assertEqual(args.rt_chunk_seconds, 12)
        self.assertEqual(args.rt_context_window_seconds, 240)
        self.assertEqual(args.rt_model, "gpt-5-mini")
        self.assertEqual(args.rt_stt_model, "gpt-4o-mini-transcribe")
        self.assertEqual(args.rt_keywords_file, "/tmp/k.json")
        self.assertEqual(args.rt_api_base_url, "https://aihubmix.com/v1")
        self.assertEqual(args.rt_request_timeout_sec, 8.0)
        self.assertEqual(args.rt_retry_count, 4)
        self.assertEqual(args.rt_alert_threshold, 88)
        self.assertEqual(args.rt_max_concurrency, 3)
        self.assertEqual(args.rt_stage_timeout_sec, 45.0)
        self.assertEqual(args.rt_context_min_ready, 10)
        self.assertEqual(args.rt_context_recent_required, 3)
        self.assertEqual(args.rt_context_wait_timeout_sec, 9.0)

    def test_simulate_mode5_profile_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "simulate",
                "--mode",
                "5",
                "--scenario-file",
                "tests/simulator/scenarios/mode5/example.yaml",
            ]
        )
        self.assertEqual(args.mode5_profile, "all_chunks_dual")
        self.assertIsNone(args.mode5_target_seq)

    def test_simulate_mode5_single_chunk_requires_target_seq(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "simulate",
                "--mode",
                "5",
                "--scenario-file",
                "tests/simulator/scenarios/mode5/example.yaml",
                "--mode5-profile",
                "single_chunk_dual",
            ]
        )
        with self.assertRaises(ValueError):
            _ = _build_runtime_config(args)

    def test_simulate_mode6_profile_ignores_mode5_target(self) -> None:
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
        cfg = _build_runtime_config(args)
        self.assertEqual(int(cfg.mode), 6)


if __name__ == "__main__":
    unittest.main()
