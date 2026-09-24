#!/usr/bin/env python3
"""Update per-account agent notes using the same live reports as /whoami.

Match by tool and login. Preserve inactive accounts' last observed values until
that quota window expires; unknown or expired usage is YAML null. Never infer
100% remaining from a reset. weekly_usage_checked_at records the observation in
UTC so a saved reading is distinguishable from live usage. No credentials are
saved. Other frontmatter and note bodies are preserved.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_accounts import EASTERN, Report, live_reports

AGENTS_DIR = Path.home() / "rust" / "hanadocs" / "agents"
RESET_FORMAT = "%Y-%m-%dT%H:%M:%S"
FIELD = re.compile(r"^([A-Za-z_][\w-]*):[ \t]*(.*?)[ \t]*$")


@dataclass
class Note:
    path: Path
    tool: str
    text: str
    start: int
    end: int

    def lines(self) -> list[str]:
        return self.text[self.start:self.end].split("\n")

    def get(self, key: str) -> str | None:
        for line in self.lines():
            match = FIELD.match(line)
            if match and match.group(1) == key:
                return match.group(2).strip("\"'")
        return None


def read_note(path: Path) -> Note | None:
    """A note with its frontmatter body located, or None if it has none."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---\n"):
        return None
    close = text.find("\n---", 3)
    if close == -1:
        return None
    return Note(path, path.stem.split(" ")[0].lower(), text, 4, close)


def local_reset(value: datetime) -> str:
    """Local wall time rounded to the minute: the APIs jitter by fractions of a second."""
    local = value.astimezone()
    rounded = (local + timedelta(seconds=30)).replace(second=0, microsecond=0)
    return rounded.strftime(RESET_FORMAT)


def set_fields(note: Note, updates: dict[str, str], stamp: dict[str, str]) -> bool:
    """Rewrite the named top-level fields in place; insert a missing one in key order.

    `stamp` is written too, but only when `updates` changed something.
    """
    lines = note.lines()
    changed = _rewrite(lines, updates)
    if not changed:
        return False
    _ = _rewrite(lines, stamp)
    text = note.text[:note.start] + "\n".join(lines) + note.text[note.end:]
    tmp = note.path.with_name(f".{note.path.name}.tmp")
    _ = tmp.write_text(text, encoding="utf-8")
    # The snapshot service runs with UMask 0077; keep the note's own mode instead.
    os.chmod(tmp, note.path.stat().st_mode & 0o777)
    os.replace(tmp, note.path)
    note.text = text
    note.end = note.start + len("\n".join(lines))
    return True


def _rewrite(lines: list[str], updates: dict[str, str]) -> bool:
    changed = False
    for key, value in updates.items():
        rendered = f"{key}: {value}"
        index = next((i for i, line in enumerate(lines)
                      if (match := FIELD.match(line)) and match.group(1) == key), None)
        if index is None:
            # Top-level keys are kept sorted; land before the first that sorts after.
            index = next((i for i, line in enumerate(lines)
                          if (match := FIELD.match(line)) and match.group(1) > key), len(lines))
            lines.insert(index, rendered)
            changed = True
        elif lines[index] != rendered:
            lines[index] = rendered
            changed = True
    return changed


def still_ahead(value: str | None) -> bool:
    if not value:
        return False
    try:
        return datetime.strptime(value, RESET_FORMAT) > datetime.now()
    except ValueError:
        return False


def apply(notes: list[Note], account: Report, checked_at: datetime) -> list[str]:
    """Write only the matching account's live quota; retain dated inactive readings."""
    messages: list[str] = []
    stamp = {"date_modified": f'"[[{checked_at.astimezone().strftime("%Y-%m-%d")}]]"'}
    reset = account.weekly_reset
    fresh = reset is not None and reset > checked_at
    remaining = account.weekly_remaining if fresh else None
    matched = False
    for note in notes:
        if note.tool != account.tool.lower():
            continue
        updates: dict[str, str] = {}
        active = bool(account.email) and (note.get("login") or "").lower() == account.email.lower()
        if account.email:
            updates["state"] = "active" if active else "inactive"
        matched = matched or active
        if active:
            if account.tool.lower() == "codex" and account.limit_reset_count is not None:
                updates["limit_reset_count"] = str(account.limit_reset_count)
                # Preserve the earliest known expiration, including its Eastern offset.
                expirations = account.limit_reset_expirations
                updates["limit_reset"] = expirations[0].astimezone(EASTERN).isoformat(timespec="seconds") if expirations else "null"
            updates["weekly_remaining_usage"] = "null" if remaining is None else f"{remaining:g}"
            if fresh:
                updates["resets"] = local_reset(reset)
            if remaining is not None:
                updates["weekly_usage_checked_at"] = checked_at.isoformat(timespec="seconds")
        elif note.get("weekly_remaining_usage") is None or not still_ahead(note.get("resets")):
            updates["weekly_remaining_usage"] = "null"
        if note.get("weekly_usage_checked_at") is None and "weekly_usage_checked_at" not in updates:
            updates["weekly_usage_checked_at"] = "null"
        if set_fields(note, updates, stamp):
            messages.append(f"{note.path.name}: " + ", ".join(f"{k}={v}" for k, v in updates.items()))
    if account.email and not matched:
        messages.append(f"{account.tool}: no note has login {account.email}")
    if account.problem or account.quota_problem:
        messages.append(f"{account.tool}: {account.problem or account.quota_problem}")
    return messages


def update() -> list[str]:
    notes = [note for path in sorted(AGENTS_DIR.glob("*.md")) if (note := read_note(path))]
    if not notes:
        return [f"no agent notes in {AGENTS_DIR}"]
    reports = asyncio.run(live_reports())
    checked_at = datetime.now(timezone.utc)
    messages: list[str] = []
    for account in reports:
        messages += apply(notes, account, checked_at)
    return messages


def main() -> None:
    for line in update():
        print(line)


if __name__ == "__main__":
    main()
