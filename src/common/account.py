from __future__ import annotations

import os
from pathlib import Path


def workspace_root() -> Path:
    # src/common/account.py -> repo root is parents[2]
    return Path(__file__).resolve().parents[2]


def default_account_file() -> Path:
    return workspace_root() / ".account"


def _parse_account_entries(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    content = path.read_text(encoding="utf-8")
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key:
            entries[key] = value
    return entries


def parse_account_file(path: Path) -> tuple[str, str]:
    entries = _parse_account_entries(path)
    username = (entries.get("username") or entries.get("user") or "").strip()
    password = (entries.get("password") or entries.get("pass") or "").strip()
    return username, password


def resolve_openai_api_key(*, env_name: str = "OPENAI_API_KEY") -> tuple[str, str]:
    resolved_env_name = (env_name or "OPENAI_API_KEY").strip() or "OPENAI_API_KEY"
    account_file = default_account_file()
    account_read_error = ""

    if account_file.exists():
        try:
            entries = _parse_account_entries(account_file)
            account_key = _read_openai_key_from_entries(entries, resolved_env_name)
            if account_key:
                return account_key, ""
        except OSError as exc:
            account_read_error = f"failed to read account file {account_file}: {exc}"

    env_key = os.environ.get(resolved_env_name, "").strip()
    if env_key:
        return env_key, ""

    if account_read_error:
        return "", account_read_error

    return "", (
        f"missing OpenAI API key: set {resolved_env_name} in {account_file} "
        f"or export {resolved_env_name}"
    )


def _read_openai_key_from_entries(entries: dict[str, str], env_name: str) -> str:
    candidates = [env_name.strip().lower(), "openai_api_key", "openai_key"]
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        value = (entries.get(candidate) or "").strip()
        if value:
            return value
    return ""


def resolve_credentials(cli_username: str, cli_password: str) -> tuple[str, str, str]:
    username = (cli_username or "").strip()
    password = (cli_password or "").strip()

    if username and password:
        return username, password, ""

    account_file = default_account_file()
    if not account_file.exists():
        return "", "", (
            f"missing credentials: provide --username/--password "
            f"or create {account_file}"
        )

    try:
        file_username, file_password = parse_account_file(account_file)
    except OSError as exc:
        return "", "", f"failed to read account file {account_file}: {exc}"

    if not username:
        username = file_username.strip()
    if not password:
        password = file_password.strip()

    if username and password:
        return username, password, ""

    return "", "", (
        f"incomplete credentials: ensure username/password are set in {account_file} "
        f"or pass them via CLI"
    )
