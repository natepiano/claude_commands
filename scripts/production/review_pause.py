#!/usr/bin/env python3
"""Pause a review's dailies, then restore them only if the review stopped them."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

if TYPE_CHECKING:
    from ..hooks import showrunner_footer as showrunner_footer
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
    import showrunner_footer as showrunner_footer


ActionState = Literal["untouched", "attempted", "done", "skipped"]


class ReviewPauseRecord(TypedDict):
    dailies: ActionState


def notifier_command() -> list[str]:
    override = os.environ.get("SHOWRUNNER_NOTIFIER")
    if override:
        return [override]
    return ["zsh", str(Path.home() / ".claude/scripts/message/notifier.sh")]


def notifier(action: str, slug: str) -> str:
    result = subprocess.run([*notifier_command(), action, f"showrunner-{slug}"],
                            capture_output=True, text=True, check=False, timeout=10)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"notifier {action} failed for {slug}")
    return result.stdout.strip()


def record_path(slug: str) -> Path:
    return showrunner_footer.review_pause_path(slug)


def read_record(path: Path) -> ReviewPauseRecord:
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise ValueError(f"invalid review pause record: {path}")
    fields = cast(dict[str, object], raw)
    try:
        # A record written while footers existed also has `footers`, which nothing reads now.
        return ReviewPauseRecord(dailies=action_state(fields["dailies"]))
    except (KeyError, ValueError) as error:
        raise ValueError(f"invalid review pause record: {path}") from error


def action_state(value: object) -> ActionState:
    # Records written before action states used booleans for completed actions.
    if value is True:
        return "done"
    if value is False:
        return "skipped"
    if value == "untouched":
        return "untouched"
    if value == "attempted":
        return "attempted"
    if value == "done":
        return "done"
    if value == "skipped":
        return "skipped"
    raise ValueError("invalid action state")


def write_record(path: Path, record: ReviewPauseRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".review-paused-", delete=False,
                                     encoding="utf-8") as temporary:
        json.dump(record, temporary)
        _ = temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def reconcile_attempted(instance: Path, path: Path, record: ReviewPauseRecord) -> bool:
    enabled = showrunner_footer.key_values(instance / "state").get("ENABLED") == "1"
    if record["dailies"] == "attempted" and not enabled:
        record["dailies"] = "done"
        write_record(path, record)
    return enabled


def pause(instance: Path) -> str:
    slug = instance.name.removeprefix("showrunner-")
    path = record_path(slug)
    existing = path.exists()
    record = read_record(path) if existing else ReviewPauseRecord(dailies="untouched")
    if not existing:
        write_record(path, record)
    enabled = reconcile_attempted(instance, path, record)
    if record["dailies"] == "untouched" and not enabled:
        record["dailies"] = "skipped"
        write_record(path, record)
    if enabled and record["dailies"] in ("untouched", "attempted"):
        record["dailies"] = "attempted"
        write_record(path, record)
        _ = notifier("stop", slug)
        record["dailies"] = "done"
        write_record(path, record)
    if existing:
        return status(instance)
    return f"{slug}: paused dailies" if record["dailies"] == "done" else f"{slug}: nothing to pause"


def status(instance: Path) -> str:
    slug = instance.name.removeprefix("showrunner-")
    path = record_path(slug)
    if not path.exists():
        return ""
    record = read_record(path)
    _ = reconcile_attempted(instance, path, record)
    return f"{slug}: review paused dailies" if record["dailies"] == "done" else f"{slug}: review paused nothing"


def resume(instance: Path, answer: str) -> str:
    slug = instance.name.removeprefix("showrunner-")
    path = record_path(slug)
    if not path.exists():
        return ""
    record = read_record(path)
    _ = reconcile_attempted(instance, path, record)
    restored = answer == "dailies" and record["dailies"] == "done"
    if restored:
        _ = notifier("start", slug)
    path.unlink()
    return f"{slug}: turned on dailies" if restored else f"{slug}: left dailies off"


def main(argv: list[str]) -> int:
    if (not argv or argv[0] not in {"pause", "status", "resume"}
            or (argv[0] == "resume" and (len(argv) != 2 or argv[1] not in {"dailies", "none"}))
            or (argv[0] != "resume" and len(argv) != 1)):
        print("usage: review_pause.py pause|status|resume dailies|none", file=sys.stderr)
        return 2
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    for instance in showrunner_footer.targeted_instances(session_id) if session_id else []:
        line = (pause(instance) if argv[0] == "pause" else status(instance) if argv[0] == "status"
                else resume(instance, argv[1]))
        if line:
            print(line)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"review_pause: {error}", file=sys.stderr)
        raise SystemExit(1) from error
