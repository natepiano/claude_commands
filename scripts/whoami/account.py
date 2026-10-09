#!/usr/bin/env python3
"""Resolve the Claude or Codex login behind a config directory or process."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast

from agent_accounts import claude_config_dir, claude_on_disk, codex_email_on_disk, codex_home
from agent_notes import AGENTS_DIR, read_note


@dataclass(frozen=True)
class Account:
    tool: Literal["claude", "codex"]
    login: str
    label: str


class AccountIdentity(TypedDict):
    login: str
    label: str


class AccountListing(TypedDict):
    claude: AccountIdentity | None
    codex: AccountIdentity | None


class CommandLine(argparse.Namespace):
    as_json: bool = False
    pid: int | None = None
    write_label: bool = False


def label_for(tool: str, login: str) -> str:
    """The matching hanadocs note stem, or the login when no note matches."""
    notes_dir = Path(os.environ.get("AGENT_NOTES_DIR", str(AGENTS_DIR)))
    folded_tool = tool.casefold()
    folded_login = login.casefold()
    for path in sorted(notes_dir.glob("*.md")):
        if not path.stem.casefold().startswith(folded_tool):
            continue
        note = read_note(path)
        if note is not None and (note.get("login") or "").casefold() == folded_login:
            return path.stem
    return login


def claude_account(config_dir: Path) -> Account | None:
    """The Claude login stored for one configuration directory."""
    report = claude_on_disk(config_dir)
    if report.email is None:
        return None
    return Account("claude", report.email, label_for("claude", report.email))


def codex_account(home: Path) -> Account | None:
    """The Codex login stored in one Codex home."""
    login = codex_email_on_disk(home)
    if login is None:
        return None
    return Account("codex", login, label_for("codex", login))


def _default_claude_config_dir() -> Path:
    return Path.home() / ".claude"


def parse_darwin_config_dir(output: str, expected_uid: int) -> Path | None:
    """Extract a config dir when `ps eww` exposed our process environment."""
    fields = output.split(maxsplit=1)
    if len(fields) != 2 or fields[0] != str(expected_uid):
        return None

    process_text = fields[1]
    if not any(token.startswith("HOME=") for token in process_text.split()):
        return None

    prefix = "CLAUDE_CONFIG_DIR="
    for token in reversed(process_text.split()):
        if token.startswith(prefix):
            return Path(token.removeprefix(prefix))
    return _default_claude_config_dir()


def process_config_dir(pid: int) -> Path | None:
    """A process's Claude config directory, or None when its environment is unreadable."""
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ("ps", "eww", "-o", "uid=,command=", "-p", str(pid)),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode:
                return None
            return parse_darwin_config_dir(result.stdout, os.getuid())
        except (OSError, subprocess.TimeoutExpired):
            return None

    try:
        environment = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    prefix = b"CLAUDE_CONFIG_DIR="
    for entry in environment.split(b"\0"):
        if entry.startswith(prefix):
            return Path(os.fsdecode(entry.removeprefix(prefix)))
    return _default_claude_config_dir()


def account_of(pid: int) -> Account | None:
    """The Claude account of a process whose environment can be read."""
    config_dir = process_config_dir(pid)
    return None if config_dir is None else claude_account(config_dir)


def write_label(config_dir: Path, account: Account) -> None:
    """Atomically refresh a config directory's cheap status-line label cache."""
    expected = account.label + "\n"
    destination = config_dir / "account-label"
    try:
        if destination.read_text(encoding="utf-8") == expected:
            return
    except OSError:
        pass

    config_dir.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_dir,
            prefix=".account-label-",
            delete=False,
        ) as output:
            temporary = output.name
            _ = output.write(expected)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _identity(account: Account | None) -> AccountIdentity | None:
    if account is None:
        return None
    return {"login": account.login, "label": account.label}


def _plain(account: Account | None) -> str:
    if account is None:
        return "unavailable"
    return f"{account.label} ({account.login})"


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--json", action="store_true", dest="as_json")
    _ = parser.add_argument("--pid", type=int)
    _ = parser.add_argument("--write-label", action="store_true")
    options = cast(CommandLine, parser.parse_args(arguments))

    current_config_dir = claude_config_dir()
    current_claude = claude_account(current_config_dir)
    claude = current_claude if options.pid is None else account_of(options.pid)
    codex = codex_account(codex_home()) if options.pid is None else None

    if options.write_label:
        if current_claude is None:
            try:
                (current_config_dir / "account-label").unlink()
            except FileNotFoundError:
                pass
        else:
            write_label(current_config_dir, current_claude)

    listing: AccountListing = {"claude": _identity(claude), "codex": _identity(codex)}
    if options.as_json:
        print(json.dumps(listing))
    elif options.pid is not None:
        print(_plain(claude))
    else:
        print(f"{_plain(claude)} · {_plain(codex)}")
    return 0 if claude is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
