from __future__ import annotations

import unittest

from src.common.billing import BillingAlertCooldown, detect_billing_issue


class BillingDetectionTests(unittest.TestCase):
    def test_detect_openai_insufficient_quota(self) -> None:
        issue = detect_billing_issue(
            service_hint="openai",
            error_text="429 insufficient_quota: You exceeded your current quota.",
        )
        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertEqual(issue.service_key, "openai")
        self.assertEqual(issue.reason_code, "billing_arrears_openai")
        self.assertIn("platform.openai.com", issue.payment_url)

    def test_detect_aihubmix_insufficient_balance(self) -> None:
        issue = detect_billing_issue(
            service_hint="openai_compatible",
            error_text="403 insufficient_user_quota: Insufficient balance.",
            api_base_url="https://aihubmix.com/v1",
        )
        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertEqual(issue.service_key, "aihubmix")
        self.assertEqual(issue.reason_code, "billing_arrears_aihubmix")
        self.assertIn("aihubmix.com", issue.payment_url)

    def test_detect_dashscope_arrearage(self) -> None:
        issue = detect_billing_issue(
            service_hint="dashscope",
            error_text="Arrearage: Access denied, please make sure your account is in good standing.",
        )
        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertEqual(issue.service_key, "dashscope")
        self.assertEqual(issue.reason_code, "billing_arrears_dashscope")

    def test_detect_tingwu_overdue(self) -> None:
        issue = detect_billing_issue(
            service_hint="tingwu",
            error_text="TeaException: BRK.OverdueTenant service status is overdue",
        )
        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertEqual(issue.service_key, "tingwu")
        self.assertEqual(issue.reason_code, "billing_arrears_tingwu")

    def test_detect_oss_account_arrearage(self) -> None:
        issue = detect_billing_issue(
            service_hint="oss",
            error_text="0003-00000806 The operation is not valid for the user account in the current billing state",
        )
        self.assertIsNotNone(issue)
        assert issue is not None
        self.assertEqual(issue.service_key, "oss")
        self.assertEqual(issue.reason_code, "billing_arrears_oss")

    def test_detect_billing_issue_returns_none_for_unrelated_error(self) -> None:
        issue = detect_billing_issue(
            service_hint="openai",
            error_text="timeout waiting for upstream service",
        )
        self.assertIsNone(issue)


class BillingCooldownTests(unittest.TestCase):
    def test_service_cooldown_isolated(self) -> None:
        gate = BillingAlertCooldown(cooldown_sec=120.0)
        ok1, remain1 = gate.consume(service_key="openai", now_mono=10.0)
        ok2, remain2 = gate.consume(service_key="openai", now_mono=60.0)
        ok3, remain3 = gate.consume(service_key="openai", now_mono=131.0)
        ok4, remain4 = gate.consume(service_key="dashscope", now_mono=60.0)

        self.assertTrue(ok1)
        self.assertEqual(remain1, 0.0)
        self.assertFalse(ok2)
        self.assertAlmostEqual(remain2, 70.0, places=3)
        self.assertTrue(ok3)
        self.assertEqual(remain3, 0.0)
        self.assertTrue(ok4)
        self.assertEqual(remain4, 0.0)


if __name__ == "__main__":
    unittest.main()
