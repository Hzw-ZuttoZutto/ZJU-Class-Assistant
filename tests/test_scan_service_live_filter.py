from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from src.scan.live_check import LiveCheckResult
from src.scan.service import run_scan


def _scan_args(require_live: bool) -> argparse.Namespace:
    return argparse.Namespace(
        username="u",
        password="p",
        tenant_code="112",
        authcode="",
        timeout=5,
        teacher="王强",
        title="高等数学",
        center=100,
        radius=0,
        workers=1,
        retries=0,
        verbose=False,
        require_live=require_live,
        live_check_timeout=30.0,
        live_check_interval=2.0,
    )


def _parse_last_json(text: str) -> dict:
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == "{":
            return json.loads("\n".join(lines[idx:]))
    raise AssertionError("no json payload found")


class ScanServiceLiveFilterTests(unittest.TestCase):
    def test_scan_without_live_filter_keeps_original_behavior(self) -> None:
        args = _scan_args(require_live=False)
        stdout = io.StringIO()

        with (
            mock.patch("src.scan.service.ZJUAuthClient.login_and_get_token", return_value="tok"),
            mock.patch(
                "src.scan.service.query_course_detail",
                return_value={"title": "高等数学", "teachers": [{"realname": "王强"}]},
            ),
            redirect_stdout(stdout),
        ):
            code = run_scan(args)

        self.assertEqual(code, 0)
        payload = _parse_last_json(stdout.getvalue())
        self.assertFalse(payload["require_live"])
        self.assertEqual(len(payload["matches"]), 1)
        self.assertEqual(payload["live_checked_candidates"], 0)
        self.assertEqual(payload["live_check_failures"], [])

    def test_scan_with_live_filter_only_keeps_live_matches(self) -> None:
        args = _scan_args(require_live=True)
        stdout = io.StringIO()

        with (
            mock.patch("src.scan.service.ZJUAuthClient.login_and_get_token", return_value="tok"),
            mock.patch(
                "src.scan.service.query_course_detail",
                return_value={"title": "高等数学", "teachers": [{"realname": "王强"}]},
            ),
            mock.patch(
                "src.scan.service.check_course_live_status",
                return_value=LiveCheckResult(
                    course_id=100,
                    is_live=True,
                    checked=True,
                    attempts=1,
                    elapsed_sec=0.1,
                    last_error="",
                    hint="dynamic_api_live_text",
                ),
            ),
            redirect_stdout(stdout),
        ):
            code = run_scan(args)

        self.assertEqual(code, 0)
        payload = _parse_last_json(stdout.getvalue())
        self.assertTrue(payload["require_live"])
        self.assertEqual(payload["live_checked_candidates"], 1)
        self.assertEqual(len(payload["matches"]), 1)
        self.assertEqual(payload["live_check_failures"], [])

    def test_scan_with_live_filter_failure_goes_to_failure_list(self) -> None:
        args = _scan_args(require_live=True)
        stdout = io.StringIO()
        stderr = io.StringIO()

        with (
            mock.patch("src.scan.service.ZJUAuthClient.login_and_get_token", return_value="tok"),
            mock.patch(
                "src.scan.service.query_course_detail",
                return_value={"title": "高等数学", "teachers": [{"realname": "王强"}]},
            ),
            mock.patch(
                "src.scan.service.check_course_live_status",
                return_value=LiveCheckResult(
                    course_id=100,
                    is_live=False,
                    checked=False,
                    attempts=15,
                    elapsed_sec=30.0,
                    last_error="dynamic_status_unavailable",
                    hint="dynamic_status_unavailable",
                ),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = run_scan(args)

        self.assertEqual(code, 0)
        payload = _parse_last_json(stdout.getvalue())
        self.assertEqual(payload["matches"], [])
        self.assertEqual(payload["live_checked_candidates"], 1)
        self.assertEqual(len(payload["live_check_failures"]), 1)
        self.assertIn("[LIVE-CHECK-FAIL]", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
