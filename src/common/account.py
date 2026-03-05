from __future__ import annotations

from pathlib import Path


def workspace_root() -> Path:
    # src/common/account.py -> repo root is parents[2]
    return Path(__file__).resolve().parents[2]


def default_account_file() -> Path:
    return workspace_root() / ".account"


def parse_account_file(path: Path) -> tuple[str, str]:
    username = ""
    password = ""
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
        if key in {"username", "user"}:
            username = value
        elif key in {"password", "pass"}:
            password = value
    return username, password


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
