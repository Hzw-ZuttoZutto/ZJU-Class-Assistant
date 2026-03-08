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
        self.assertEqual(args.rt_alert_threshold, 90)
        self.assertFalse(args.rt_dingtalk_enabled)
        self.assertEqual(args.rt_dingtalk_cooldown_sec, 30.0)
        self.assertEqual(args.rt_max_concurrency, 5)
        self.assertEqual(args.rt_context_min_ready, 15)
        self.assertEqual(args.rt_context_recent_required, 4)
        self.assertEqual(args.rt_context_wait_timeout_sec_1, 1.0)
        self.assertEqual(args.rt_context_wait_timeout_sec_2, 5.0)

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
                "whisper-large-v3",
                "--rt-keywords-file",
                "/tmp/k.json",
                "--rt-api-base-url",
                "https://aihubmix.com/v1",
                "--rt-stt-request-timeout-sec",
                "8",
                "--rt-stt-stage-timeout-sec",
                "45",
                "--rt-stt-retry-count",
                "4",
                "--rt-stt-retry-interval-sec",
                "0.3",
                "--rt-analysis-request-timeout-sec",
                "11",
                "--rt-analysis-stage-timeout-sec",
                "50",
                "--rt-analysis-retry-count",
                "5",
                "--rt-analysis-retry-interval-sec",
                "0.4",
                "--rt-alert-threshold",
                "88",
                "--rt-dingtalk-enabled",
                "--rt-dingtalk-cooldown-sec",
                "45",
                "--rt-max-concurrency",
                "3",
                "--rt-context-min-ready",
                "10",
                "--rt-context-recent-required",
                "3",
                "--rt-context-wait-timeout-sec-1",
                "2",
                "--rt-context-wait-timeout-sec-2",
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
        self.assertEqual(args.rt_stt_model, "whisper-large-v3")
        self.assertEqual(args.rt_keywords_file, "/tmp/k.json")
        self.assertEqual(args.rt_api_base_url, "https://aihubmix.com/v1")
        self.assertEqual(args.rt_stt_request_timeout_sec, 8.0)
        self.assertEqual(args.rt_stt_stage_timeout_sec, 45.0)
        self.assertEqual(args.rt_stt_retry_count, 4)
        self.assertEqual(args.rt_stt_retry_interval_sec, 0.3)
        self.assertEqual(args.rt_analysis_request_timeout_sec, 11.0)
        self.assertEqual(args.rt_analysis_stage_timeout_sec, 50.0)
        self.assertEqual(args.rt_analysis_retry_count, 5)
        self.assertEqual(args.rt_analysis_retry_interval_sec, 0.4)
        self.assertEqual(args.rt_alert_threshold, 88)
        self.assertTrue(args.rt_dingtalk_enabled)
        self.assertEqual(args.rt_dingtalk_cooldown_sec, 45.0)
        self.assertEqual(args.rt_max_concurrency, 3)
        self.assertEqual(args.rt_context_min_ready, 10)
        self.assertEqual(args.rt_context_recent_required, 3)
        self.assertEqual(args.rt_context_wait_timeout_sec_1, 2.0)
        self.assertEqual(args.rt_context_wait_timeout_sec_2, 9.0)

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
