from __future__ import annotations

import unittest
from unittest import mock

import requests

from src.scan.live_check import LiveCheckResult, check_course_live_status


class _Resp:
    def __init__(self, text: str = "", payload: dict | None = None, status_code: int = 200) -> None:
        self.text = text
        self._payload = payload
        self.status_code = status_code
        self.encoding = "utf-8"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}")

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class LiveCheckTests(unittest.TestCase):
    def test_check_live_static_html_hit(self) -> None:
        session = mock.Mock(spec=requests.Session)
        session.get.side_effect = [_Resp(text="<html>...直播中...</html>")]

        result = check_course_live_status(
            session=session,
            token="tok",
            timeout=5,
            tenant_code="112",
            course_id=1,
            max_wait_sec=30.0,
            interval_sec=2.0,
        )
        self.assertIsInstance(result, LiveCheckResult)
        self.assertTrue(result.checked)
        self.assertTrue(result.is_live)
        self.assertEqual(result.hint, "static_html_live_text")

    def test_check_live_dynamic_api_no_live(self) -> None:
        session = mock.Mock(spec=requests.Session)
        session.get.side_effect = [
            _Resp(text="<html>dynamic app shell</html>"),
            _Resp(payload={"code": 0, "msg": "ok", "list": [{"status_text": "未开始"}]}),
        ]

        result = check_course_live_status(
            session=session,
            token="tok",
            timeout=5,
            tenant_code="112",
            course_id=1,
            max_wait_sec=30.0,
            interval_sec=2.0,
        )
        self.assertTrue(result.checked)
        self.assertFalse(result.is_live)
        self.assertEqual(result.hint, "dynamic_api_no_live_text")

    def test_check_live_timeout_failure(self) -> None:
        session = mock.Mock(spec=requests.Session)
        session.get.side_effect = requests.RequestException("boom")

        result = check_course_live_status(
            session=session,
            token="tok",
            timeout=5,
            tenant_code="112",
            course_id=1,
            max_wait_sec=0.0,
            interval_sec=0.0,
        )
        self.assertFalse(result.checked)
        self.assertFalse(result.is_live)
        self.assertGreaterEqual(result.attempts, 1)
        self.assertIn("error", result.last_error)


if __name__ == "__main__":
    unittest.main()
