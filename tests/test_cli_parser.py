from __future__ import annotations

import unittest

from src.cli.parser import build_parser


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

    def test_analysis_args_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "analysis",
                "--course-id",
                "1",
                "--sub-id",
                "2",
            ]
        )
        self.assertEqual(args.output_dir, "")
        self.assertEqual(args.username, "")
        self.assertEqual(args.password, "")
        self.assertEqual(args.poll_interval, 10.0)
        self.assertEqual(args.rt_model, "gpt-4.1-mini")
        self.assertEqual(args.rt_asr_scene, "zh")
        self.assertIsNone(args.rt_asr_model)
        self.assertEqual(args.rt_hotwords_file, "config/realtime_hotwords.json")
        self.assertEqual(args.rt_window_sentences, 8)
        self.assertEqual(args.rt_stream_analysis_workers, 32)
        self.assertEqual(args.rt_stream_queue_size, 100)
        self.assertEqual(args.rt_asr_endpoint, "wss://dashscope.aliyuncs.com/api-ws/v1/inference")
        self.assertEqual(args.rt_translation_target_languages, "zh")
        self.assertEqual(args.rt_keywords_file, "config/realtime_keywords.json")
        self.assertEqual(args.rt_api_base_url, "")
        self.assertEqual(args.rt_analysis_request_timeout_sec, 15.0)
        self.assertEqual(args.rt_analysis_stage_timeout_sec, 60.0)
        self.assertEqual(args.rt_analysis_retry_count, 4)
        self.assertEqual(args.rt_analysis_retry_interval_sec, 0.2)
        self.assertEqual(args.rt_alert_threshold, 90)
        self.assertFalse(args.rt_dingtalk_enabled)
        self.assertEqual(args.rt_dingtalk_cooldown_sec, 30.0)
        self.assertEqual(args.rt_dingtalk_queue_size, 500)
        self.assertEqual(args.rt_context_recent_required, 4)
        self.assertEqual(args.rt_context_wait_timeout_sec_1, 1.0)
        self.assertEqual(args.rt_context_wait_timeout_sec_2, 5.0)
        self.assertEqual(args.rt_log_rotate_max_bytes, 64 * 1024 * 1024)
        self.assertEqual(args.rt_log_rotate_backup_count, 20)
        self.assertFalse(args.tingwu_enabled)
        self.assertEqual(args.tingwu_poll_interval_sec, 30.0)
        self.assertEqual(args.tingwu_max_wait_hours, 6.0)

    def test_analysis_args_custom(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "analysis",
                "--username",
                "u",
                "--password",
                "p",
                "--course-id",
                "1",
                "--sub-id",
                "2",
                "--output-dir",
                "/tmp/r",
                "--poll-interval",
                "3",
                "--rt-model",
                "gpt-5-mini",
                "--rt-asr-scene",
                "multi",
                "--rt-asr-model",
                "gummy-realtime-v1",
                "--rt-hotwords-file",
                "/tmp/hotwords.json",
                "--rt-window-sentences",
                "9",
                "--rt-stream-analysis-workers",
                "40",
                "--rt-stream-queue-size",
                "120",
                "--rt-asr-endpoint",
                "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
                "--rt-translation-target-languages",
                "zh,en",
                "--rt-keywords-file",
                "/tmp/k.json",
                "--rt-api-base-url",
                "https://aihubmix.com/v1",
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
                "--rt-dingtalk-queue-size",
                "600",
                "--rt-context-recent-required",
                "3",
                "--rt-context-wait-timeout-sec-1",
                "2",
                "--rt-context-wait-timeout-sec-2",
                "9",
                "--rt-log-rotate-max-bytes",
                "1048576",
                "--rt-log-rotate-backup-count",
                "7",
                "--tingwu-enabled",
                "--tingwu-poll-interval-sec",
                "45",
                "--tingwu-max-wait-hours",
                "8",
            ]
        )
        self.assertEqual(args.output_dir, "/tmp/r")
        self.assertEqual(args.poll_interval, 3.0)
        self.assertEqual(args.rt_model, "gpt-5-mini")
        self.assertEqual(args.rt_asr_scene, "multi")
        self.assertEqual(args.rt_asr_model, "gummy-realtime-v1")
        self.assertEqual(args.rt_hotwords_file, "/tmp/hotwords.json")
        self.assertEqual(args.rt_window_sentences, 9)
        self.assertEqual(args.rt_stream_analysis_workers, 40)
        self.assertEqual(args.rt_stream_queue_size, 120)
        self.assertEqual(args.rt_asr_endpoint, "wss://dashscope.aliyuncs.com/api-ws/v1/inference")
        self.assertEqual(args.rt_translation_target_languages, "zh,en")
        self.assertEqual(args.rt_keywords_file, "/tmp/k.json")
        self.assertEqual(args.rt_api_base_url, "https://aihubmix.com/v1")
        self.assertEqual(args.rt_analysis_request_timeout_sec, 11.0)
        self.assertEqual(args.rt_analysis_stage_timeout_sec, 50.0)
        self.assertEqual(args.rt_analysis_retry_count, 5)
        self.assertEqual(args.rt_analysis_retry_interval_sec, 0.4)
        self.assertEqual(args.rt_alert_threshold, 88)
        self.assertTrue(args.rt_dingtalk_enabled)
        self.assertEqual(args.rt_dingtalk_cooldown_sec, 45.0)
        self.assertEqual(args.rt_dingtalk_queue_size, 600)
        self.assertEqual(args.rt_context_recent_required, 3)
        self.assertEqual(args.rt_context_wait_timeout_sec_1, 2.0)
        self.assertEqual(args.rt_context_wait_timeout_sec_2, 9.0)
        self.assertEqual(args.rt_log_rotate_max_bytes, 1048576)
        self.assertEqual(args.rt_log_rotate_backup_count, 7)
        self.assertTrue(args.tingwu_enabled)
        self.assertEqual(args.tingwu_poll_interval_sec, 45.0)
        self.assertEqual(args.tingwu_max_wait_hours, 8.0)

    def test_watch_subcommand_removed(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit) as raised:
            parser.parse_args(["watch"])
        self.assertEqual(raised.exception.code, 2)

    def test_auto_analysis_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "auto-analysis",
                "--config",
                "config/auto_analysis.json",
            ]
        )
        self.assertEqual(args.command, "auto-analysis")
        self.assertEqual(args.config, "config/auto_analysis.json")
        self.assertEqual(args.username, "")
        self.assertEqual(args.password, "")
        self.assertEqual(args.tenant_code, "112")
        self.assertEqual(args.authcode, "")
        self.assertEqual(args.timeout, 20)

    def test_tingwu_process_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "tingwu-process",
                "--job-file",
                "/tmp/tingwu_job.json",
            ]
        )
        self.assertEqual(args.command, "tingwu-process")
        self.assertEqual(args.job_file, "/tmp/tingwu_job.json")


if __name__ == "__main__":
    unittest.main()
