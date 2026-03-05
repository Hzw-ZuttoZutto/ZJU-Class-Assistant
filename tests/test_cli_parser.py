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
            ]
        )
        self.assertEqual(args.record_dir, "/tmp/r")
        self.assertEqual(args.record_segment_minutes, 0)
        self.assertEqual(args.record_startup_av_timeout, 20.0)
        self.assertEqual(args.record_recovery_window_sec, 7.0)


if __name__ == "__main__":
    unittest.main()
