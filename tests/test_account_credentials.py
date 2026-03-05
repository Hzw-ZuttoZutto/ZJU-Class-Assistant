from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.common.account import parse_account_file, resolve_credentials


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


if __name__ == "__main__":
    unittest.main()
