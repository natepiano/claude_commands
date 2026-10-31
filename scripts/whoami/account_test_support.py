"""Synthetic Codex sign-in files for account and launcher tests."""

import base64
import json
from pathlib import Path


def write_codex_auth(home: Path, login: str) -> None:
    """Write a fake ID token identifying the login in an existing Codex home."""
    claims = base64.urlsafe_b64encode(json.dumps({"email": login}).encode()).decode().rstrip("=")
    _ = (home / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": f"header.{claims}.signature"}})
    )
