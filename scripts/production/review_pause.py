#!/usr/bin/env python3
"""Pause a review's dailies and footers, then restore only what it changed."""

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
    footers: ActionState


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
    return showrunner_footer.state_root() / "review-paused" / f"{slug}.json"


def read_record(path: Path) -> ReviewPauseRecord:
    raw = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw, dict):
        raise ValueError(f"invalid review pause record: {path}")
    fields = cast(dict[str, object], raw)
    try:
        return ReviewPauseRecord(dailies=action_state(fields["dailies"]),
                                 footers=action_state(fields["footers"]))
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


def parts(record: ReviewPauseRecord) -> list[str]:
    return [name for name in ("dailies", "footers") if record[name] == "done"]


def reconcile_attempted(instance: Path, slug: str, path: Path,
                        record: ReviewPauseRecord) -> tuple[bool, bool]:
    enabled = showrunner_footer.key_values(instance / "state").get("ENABLED") == "1"
    footers_on = showrunner_footer.footer_state(slug) is showrunner_footer.FooterState.ON
    changed = False
    if record["dailies"] == "attempted" and not enabled:
        record["dailies"] = "done"
        changed = True
    if record["footers"] == "attempted" and not footers_on:
        record["footers"] = "done"
        changed = True
    if changed:
        write_record(path, record)
    return enabled, footers_on


def pause(instance: Path) -> str:
    slug = instance.name.removeprefix("showrunner-")
    path = record_path(slug)
    existing = path.exists()
    record = read_record(path) if existing else ReviewPauseRecord(dailies="untouched", footers="untouched")
    if not existing:
        write_record(path, record)
    enabled, footers_on = reconcile_attempted(instance, slug, path, record)
    if record["dailies"] == "untouched" and not enabled:
        record["dailies"] = "skipped"
        write_record(path, record)
    if enabled and record["dailies"] in ("untouched", "attempted"):
        record["dailies"] = "attempted"
        write_record(path, record)
        _ = notifier("stop", slug)
        record["dailies"] = "done"
        write_record(path, record)
    if record["footers"] == "untouched" and not footers_on:
        record["footers"] = "skipped"
        write_record(path, record)
    if footers_on and record["footers"] in ("untouched", "attempted"):
        record["footers"] = "attempted"
        write_record(path, record)
        showrunner_footer.set_footer_state(slug, showrunner_footer.FooterState.OFF)
        record["footers"] = "done"
        write_record(path, record)
    if existing:
        return status(instance)
    if record["dailies"] == "done" and record["footers"] == "done":
        return f"{slug}: paused dailies and footers"
    if record["footers"] == "done":
        return f"{slug}: paused footers; dailies were already off"
    if record["dailies"] == "done":
        return f"{slug}: paused dailies; footers were already off"
    return f"{slug}: nothing to pause"


def status(instance: Path) -> str:
    slug = instance.name.removeprefix("showrunner-")
    path = record_path(slug)
    if not path.exists():
        return ""
    record = read_record(path)
    _ = reconcile_attempted(instance, slug, path, record)
    changed = parts(record)
    return f"{slug}: review paused {', '.join(changed)}" if changed else f"{slug}: review paused nothing"


def resume(instance: Path, answer: str) -> str:
    slug = instance.name.removeprefix("showrunner-")
    path = record_path(slug)
    if not path.exists():
        return ""
    record = read_record(path)
    _ = reconcile_attempted(instance, slug, path, record)
    choices: dict[str, set[str]] = {"both": {"dailies", "footers"}, "dailies": {"dailies"},
                                    "footers": {"footers"}, "none": set()}
    selected = choices[answer]
    restored: list[str] = []
    if "dailies" in selected and record["dailies"] == "done":
        _ = notifier("start", slug)
        restored.append("dailies")
    if "footers" in selected and record["footers"] == "done":
        showrunner_footer.set_footer_state(slug, showrunner_footer.FooterState.ON)
        restored.append("footers")
    path.unlink()
    return f"{slug}: turned on {', '.join(restored)}" if restored else f"{slug}: left dailies and footers off"


def main(argv: list[str]) -> int:
    if (not argv or argv[0] not in {"pause", "status", "resume"}
            or (argv[0] == "resume" and (len(argv) != 2 or argv[1] not in
                                         {"both", "dailies", "footers", "none"}))
            or (argv[0] != "resume" and len(argv) != 1)):
        print("usage: review_pause.py pause|status|resume both|dailies|footers|none", file=sys.stderr)
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
