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
    key, _, err = resolve_openai_client_settings(api_key_env_name=env_name, base_url_env_name="OPENAI_BASE_URL")
    return key, err


def resolve_openai_base_url(*, env_name: str = "OPENAI_BASE_URL") -> str:
    _, base_url, _ = resolve_openai_client_settings(
        api_key_env_name="OPENAI_API_KEY",
        base_url_env_name=env_name,
    )
    return base_url


def resolve_openai_client_settings(
    *,
    api_key_env_name: str = "OPENAI_API_KEY",
    base_url_env_name: str = "OPENAI_BASE_URL",
) -> tuple[str, str, str]:
    resolved_api_env = (api_key_env_name or "OPENAI_API_KEY").strip() or "OPENAI_API_KEY"
    resolved_base_env = (base_url_env_name or "OPENAI_BASE_URL").strip() or "OPENAI_BASE_URL"

    account_file = default_account_file()
    account_entries: dict[str, str] = {}
    account_read_error = ""
    if account_file.exists():
        try:
            account_entries = _parse_account_entries(account_file)
        except OSError as exc:
            account_read_error = f"failed to read account file {account_file}: {exc}"

    key, key_source = _resolve_openai_key(
        account_entries=account_entries,
        api_key_env_name=resolved_api_env,
    )
    base_url = _resolve_openai_base_url(
        account_entries=account_entries,
        base_url_env_name=resolved_base_env,
    )

    if not key:
        if account_read_error:
            return "", "", account_read_error
        return "", "", (
            f"missing OpenAI API key: set {resolved_api_env} / AIHUBMIX_API_KEY in {account_file} "
            f"or export {resolved_api_env} / AIHUBMIX_API_KEY"
        )

    if not base_url and key_source == "aihubmix":
        base_url = "https://aihubmix.com/v1"
    return key, base_url, ""


def resolve_dingtalk_bot_settings(
    *,
    webhook_env_name: str = "DINGTALK_WEBHOOK",
    secret_env_name: str = "DINGTALK_SECRET",
) -> tuple[str, str, str]:
    resolved_webhook_env = (webhook_env_name or "DINGTALK_WEBHOOK").strip() or "DINGTALK_WEBHOOK"
    resolved_secret_env = (secret_env_name or "DINGTALK_SECRET").strip() or "DINGTALK_SECRET"

    account_file = default_account_file()
    account_entries: dict[str, str] = {}
    account_read_error = ""
    if account_file.exists():
        try:
            account_entries = _parse_account_entries(account_file)
        except OSError as exc:
            account_read_error = f"failed to read account file {account_file}: {exc}"

    webhook = _resolve_named_setting(
        account_entries=account_entries,
        account_candidates=[resolved_webhook_env.lower(), "dingtalk_webhook"],
        env_candidates=[resolved_webhook_env],
    )
    secret = _resolve_named_setting(
        account_entries=account_entries,
        account_candidates=[resolved_secret_env.lower(), "dingtalk_secret"],
        env_candidates=[resolved_secret_env],
    )

    if webhook and secret:
        return webhook, secret, ""
    if account_read_error:
        return "", "", account_read_error

    missing_parts: list[str] = []
    if not webhook:
        missing_parts.append(
            f"webhook: set {resolved_webhook_env} or dingtalk_webhook in {account_file}"
        )
    if not secret:
        missing_parts.append(
            f"secret: set {resolved_secret_env} or dingtalk_secret in {account_file}"
        )
    return "", "", "missing DingTalk bot settings: " + "; ".join(missing_parts)


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


def _resolve_openai_key(
    *,
    account_entries: dict[str, str],
    api_key_env_name: str,
) -> tuple[str, str]:
    account_candidates = [
        (api_key_env_name.strip().lower(), "openai"),
        ("openai_api_key", "openai"),
        ("openai_key", "openai"),
        ("aihubmix_api_key", "aihubmix"),
    ]
    for key_name, source in account_candidates:
        if not key_name:
            continue
        value = (account_entries.get(key_name) or "").strip()
        if value:
            return value, source

    env_candidates = [
        (api_key_env_name.strip(), "openai"),
        ("AIHUBMIX_API_KEY", "aihubmix"),
    ]
    for key_name, source in env_candidates:
        if not key_name:
            continue
        value = os.environ.get(key_name, "").strip()
        if value:
            return value, source
    return "", ""


def _resolve_openai_base_url(
    *,
    account_entries: dict[str, str],
    base_url_env_name: str,
) -> str:
    account_candidates = [
        base_url_env_name.strip().lower(),
        "openai_base_url",
        "aihubmix_base_url",
        "base_url",
    ]
    for key_name in account_candidates:
        if not key_name:
            continue
        value = (account_entries.get(key_name) or "").strip()
        if value:
            return value

    env_candidates = [base_url_env_name.strip(), "AIHUBMIX_BASE_URL"]
    for key_name in env_candidates:
        if not key_name:
            continue
        value = os.environ.get(key_name, "").strip()
        if value:
            return value
    return ""


def _resolve_named_setting(
    *,
    account_entries: dict[str, str],
    account_candidates: list[str],
    env_candidates: list[str],
) -> str:
    seen_account: set[str] = set()
    for key_name in account_candidates:
        candidate = key_name.strip().lower()
        if not candidate or candidate in seen_account:
            continue
        seen_account.add(candidate)
        value = (account_entries.get(candidate) or "").strip()
        if value:
            return value

    seen_env: set[str] = set()
    for key_name in env_candidates:
        candidate = key_name.strip()
        if not candidate or candidate in seen_env:
            continue
        seen_env.add(candidate)
        value = os.environ.get(candidate, "").strip()
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
