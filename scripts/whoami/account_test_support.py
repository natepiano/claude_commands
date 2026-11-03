"""Synthetic sign-in files for account and launcher tests."""

from datetime import datetime, timezone
import base64
import json
from pathlib import Path


def write_codex_auth(home: Path, login: str) -> None:
    """Write a fake ID token identifying the login in an existing Codex home."""
    claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
    _ = (home / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}})
    )


def write_claude_account(label: str, login: str, left: float, reset: datetime) -> None:
    """Create a Claude account with a fresh stored quota reading."""
    from scripts.accounts import accounts
    from scripts.whoami import note_file
    from scripts.whoami.quota_alert_test_support import stored_room

    notes = note_file.notes_dir()
    notes.mkdir(parents=True, exist_ok=True)
    _ = (notes / f"{label}.md").write_text(f"---\nlogin: {login}\n---\n")
    problems = accounts.setup(label)
    if problems:
        raise AssertionError(problems)
    _ = (accounts.account_dir(label) / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": login},
    }))
    stored_room(login, left, (reset - datetime.now(timezone.utc)).total_seconds() / 86400)
