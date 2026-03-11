from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.common.account import (
    parse_account_file,
    resolve_credentials,
    resolve_dingtalk_bot_settings,
    resolve_openai_api_key,
    resolve_openai_client_settings,
    resolve_tingwu_settings,
)


class AccountCredentialTests(unittest.TestCase):
    def test_parse_account_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".account"
            path.write_text("USERNAME=alice\nPASSWORD=secret\n", encoding="utf-8")
            username, password = parse_account_file(path)
            self.assertEqual(username, "alice")
            self.assertEqual(password, "secret")

    def test_resolve_credentials_prefers_cli(self) -> None:
        username, password, err = resolve_credentials("cli_user", "cli_pass")
        self.assertEqual(username, "cli_user")
        self.assertEqual(password, "cli_pass")
        self.assertEqual(err, "")

    def test_resolve_credentials_from_account_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".account"
            path.write_text("USERNAME=file_user\nPASSWORD=file_pass\n", encoding="utf-8")
            with mock.patch("src.common.account.default_account_file", return_value=path):
                username, password, err = resolve_credentials("", "")
        self.assertEqual(username, "file_user")
        self.assertEqual(password, "file_pass")
        self.assertEqual(err, "")

    def test_resolve_credentials_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".account"
            with mock.patch("src.common.account.default_account_file", return_value=path):
                username, password, err = resolve_credentials("", "")
        self.assertEqual(username, "")
        self.assertEqual(password, "")
        self.assertIn("missing credentials", err)

    def test_resolve_openai_api_key_from_account_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".account"
            path.write_text("OPENAI_API_KEY=file_key\n", encoding="utf-8")
            with (
                mock.patch("src.common.account.default_account_file", return_value=path),
                mock.patch.dict("os.environ", {"OPENAI_API_KEY": "env_key"}, clear=True),
            ):
                key, err = resolve_openai_api_key()
        self.assertEqual(key, "file_key")
        self.assertEqual(err, "")

    def test_resolve_openai_api_key_fallback_to_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".account"
            with (
                mock.patch("src.common.account.default_account_file", return_value=path),
                mock.patch.dict("os.environ", {"OPENAI_API_KEY": "env_key"}, clear=True),
            ):
                key, err = resolve_openai_api_key()
        self.assertEqual(key, "env_key")
        self.assertEqual(err, "")

    def test_resolve_openai_api_key_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".account"
            with (
                mock.patch("src.common.account.default_account_file", return_value=path),
                mock.patch.dict("os.environ", {}, clear=True),
            ):
                key, err = resolve_openai_api_key()
        self.assertEqual(key, "")
        self.assertIn("missing OpenAI API key", err)

    def test_resolve_openai_client_settings_aihubmix_defaults_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".account"
            path.write_text("AIHUBMIX_API_KEY=ahm_key\n", encoding="utf-8")
            with (
                mock.patch("src.common.account.default_account_file", return_value=path),
                mock.patch.dict("os.environ", {}, clear=True),
            ):
                key, base_url, err = resolve_openai_client_settings()
        self.assertEqual(key, "ahm_key")
        self.assertEqual(base_url, "https://aihubmix.com/v1")
        self.assertEqual(err, "")

    def test_resolve_openai_client_settings_respects_explicit_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".account"
            path.write_text(
                "AIHUBMIX_API_KEY=ahm_key\nOPENAI_BASE_URL=https://example.gateway/v1\n",
                encoding="utf-8",
            )
            with (
                mock.patch("src.common.account.default_account_file", return_value=path),
                mock.patch.dict("os.environ", {}, clear=True),
            ):
                key, base_url, err = resolve_openai_client_settings()
        self.assertEqual(key, "ahm_key")
        self.assertEqual(base_url, "https://example.gateway/v1")
        self.assertEqual(err, "")

    def test_resolve_dingtalk_bot_settings_from_account_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".account"
            path.write_text(
                "DINGTALK_WEBHOOK=https://example.test/hook\nDINGTALK_SECRET=sec-123\n",
                encoding="utf-8",
            )
            with (
                mock.patch("src.common.account.default_account_file", return_value=path),
                mock.patch.dict(
                    "os.environ",
                    {"DINGTALK_WEBHOOK": "https://env.test/hook", "DINGTALK_SECRET": "env-sec"},
                    clear=True,
                ),
            ):
                webhook, secret, err = resolve_dingtalk_bot_settings()
        self.assertEqual(webhook, "https://example.test/hook")
        self.assertEqual(secret, "sec-123")
        self.assertEqual(err, "")

    def test_resolve_dingtalk_bot_settings_fallback_to_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".account"
            with (
                mock.patch("src.common.account.default_account_file", return_value=path),
                mock.patch.dict(
                    "os.environ",
                    {"DINGTALK_WEBHOOK": "https://env.test/hook", "DINGTALK_SECRET": "env-sec"},
                    clear=True,
                ),
            ):
                webhook, secret, err = resolve_dingtalk_bot_settings()
        self.assertEqual(webhook, "https://env.test/hook")
        self.assertEqual(secret, "env-sec")
        self.assertEqual(err, "")

    def test_resolve_tingwu_settings_from_account_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".account"
            path.write_text(
                "\n".join(
                    [
                        "ALIBABA_CLOUD_ACCESS_KEY_ID=ak",
                        "ALIBABA_CLOUD_ACCESS_KEY_SECRET=sk",
                        "TINGWU_APP_KEY=app",
                        "TINGWU_OSS_BUCKET=bucket",
                        "TINGWU_OSS_REGION=cn-beijing",
                        "TINGWU_OSS_ENDPOINT=https://oss-cn-beijing.aliyuncs.com/",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with (
                mock.patch("src.common.account.default_account_file", return_value=path),
                mock.patch.dict("os.environ", {}, clear=True),
            ):
                settings, err = resolve_tingwu_settings()
        self.assertEqual(err, "")
        assert settings is not None
        self.assertEqual(settings.access_key_id, "ak")
        self.assertEqual(settings.access_key_secret, "sk")
        self.assertEqual(settings.app_key, "app")
        self.assertEqual(settings.oss_bucket, "bucket")
        self.assertEqual(settings.oss_region, "cn-beijing")
        self.assertEqual(settings.oss_endpoint, "oss-cn-beijing.aliyuncs.com")

    def test_resolve_tingwu_settings_requires_standard_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / ".account"
            path.write_text(
                "\n".join(
                    [
                        "ACCESS_KEY_ID=legacy-ak",
                        "ACCESS_KEY_SECRET=legacy-sk",
                        "TINGWU_OSS_BUCKET=bucket",
                        "TINGWU_OSS_REGION=cn-beijing",
                        "TINGWU_OSS_ENDPOINT=oss-cn-beijing.aliyuncs.com",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with (
                mock.patch("src.common.account.default_account_file", return_value=path),
                mock.patch.dict("os.environ", {}, clear=True),
            ):
                settings, err = resolve_tingwu_settings()
        self.assertIsNone(settings)
        self.assertIn("ALIBABA_CLOUD_ACCESS_KEY_ID", err)
        self.assertIn("ALIBABA_CLOUD_ACCESS_KEY_SECRET", err)
        self.assertIn("TINGWU_APP_KEY", err)


if __name__ == "__main__":
    unittest.main()
