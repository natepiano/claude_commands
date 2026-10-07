#!/usr/bin/env python3
"""The nightly review: two read-only agents, one improvement proposal each, offered in the morning.

`launch` runs from the nightly-review timer on natedev (/etc/nixos/modules/linux/nightly-review.nix).
It starts `/nightly_review config` and `/nightly_review rust` in named, detached tmux sessions with
Remote Control under the same names, replacing the previous night's unless the user is attached to
it. A session under that name that no nightly review started is left running, and that review skips
the night. Each agent writes NIGHT/<mode>.md and messages natedev, which distills the proposals into
NIGHT/digest.md and the ledger (/watcher). The night is skipped while the active Claude account has
less than MIN_REMAINING percent of its weekly usage left.

`offer` is a UserPromptSubmit hook in /etc/nixos/.claude/settings.json. On the natedev session's
first prompt from 05:00, once per night, it tells the session what the night produced, plus the
ledger's deferred proposals, so it asks the user whether to see them now or defer them.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from datetime import date, datetime, time
from pathlib import Path

# The quota readers live with the agent notes, one directory over.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "whoami"))
from quota_alert import ahead, current_session, remaining  # noqa: E402

ROOT = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "nightly-review"
MODES = {"config": Path("/etc/nixos"), "rust": Path.home() / "rust"}
SETTINGS = Path(__file__).with_name("settings.json")
MIN_REMAINING = 10.0
OFFER_FROM = time(5)
OWNER = "natedev"


def night_dir(day: date) -> Path:
    return ROOT / day.isoformat()


def session(mode: str) -> str:
    return f"nightly-{mode}"


def tmux(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["tmux", *args], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(["tmux", *args], 127, "", "tmux is not installed")


def running(mode: str) -> bool:
    return tmux("has-session", "-t", f"={session(mode)}").returncode == 0


def quota_block() -> str | None:
    """Why tonight is skipped, or None to run."""
    from agent_notes import read_notes

    for note in read_notes():
        left = remaining(note)
        if (note.tool == "claude" and note.get("state") == "active" and left is not None
                and left < MIN_REMAINING and ahead(note.get("resets"))):
            return f"{note.path.stem} has {left:g}% of its weekly usage left, under the {MIN_REMAINING:g}% floor"
    return None


def start(mode: str) -> str:
    name = session(mode)
    if running(mode):
        if tmux("list-clients", "-t", f"={name}").stdout.strip():
            return f"{name}: you are attached, so last night's session was left running"
        # A session promoted out of a nightly review keeps its tmux name.
        if f"/nightly_review {mode}" not in tmux("list-panes", "-F", "#{pane_start_command}", "-t", f"={name}:").stdout:
            return f"{name}: another session holds this name, so tonight's review did not start"
        _ = tmux("kill-session", "-t", f"={name}")
    claude = shlex.join(["claude", "--remote-control", name, "-n", name, "--add-dir", str(Path.home() / ".claude"),
                         "--settings", str(SETTINGS), f"/nightly_review {mode}"])
    # A scope of its own, so a tmux server this starts outlives the timer's oneshot unit.
    started = subprocess.run(
        ["systemd-run", "--user", "--scope", "--quiet", "--collect", "tmux", "new-session", "-d", "-s", name,
         "-c", str(MODES[mode]), "zsh", "-ic", f"ENABLE_TOOL_SEARCH=true command {claude}; exec zsh"],
        capture_output=True, text=True, check=False)
    return f"{name}: started" if started.returncode == 0 else f"{name}: failed to start: {started.stderr.strip()}"


def launch(today: date) -> list[str]:
    directory = night_dir(today)
    directory.mkdir(parents=True, exist_ok=True)
    reason = quota_block()
    lines = [f"skipped: {reason}"] if reason else [start(mode) for mode in MODES]
    _ = (directory / "launch.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def status(directory: Path, mode: str) -> str:
    if (directory / f"{mode}.md").exists():
        return f"{mode}.md is ready"
    if running(mode):
        return f"{mode} is still running (tmux session {session(mode)})"
    return f"{mode} ended without a proposal"


def offer(now: datetime, here: Callable[[], str | None] = current_session) -> str | None:
    """The context to hand the session, or None when there is nothing to offer.

    It runs on every prompt of every session in /etc/nixos, so the session lookup, which walks
    the process tree, comes after the file checks.
    """
    directory = night_dir(now.date())
    log = directory / "launch.log"
    marker = directory / "offered"
    if now.time() < OFFER_FROM or not log.exists() or marker.exists() or here() != OWNER:
        return None
    context = summary(directory, log.read_text(encoding="utf-8").strip())
    marker.touch()
    return context


ASK = ("Before anything else this turn, ask the user once whether they want to see {what} now or defer it, "
       "and on yes present it with /nightly_next. On defer, change its ledger line's `— proposed` to `— deferred` "
       "(/watcher, nightly review rule).")


def deferred() -> list[str]:
    """Ledger lines ending `— deferred`; each morning's offer asks about them again."""
    ledger = ROOT / "ledger.md"
    if not ledger.exists():
        return []
    return [line.removeprefix("- ").removesuffix(" — deferred")
            for line in ledger.read_text(encoding="utf-8").splitlines() if line.endswith(" — deferred")]


def summary(directory: Path, launched: str) -> str:
    held = deferred()
    earlier = f" Deferred earlier: {'; '.join(held)}." if held else ""
    if launched.startswith("skipped:"):
        ask = " " + ASK.format(what="the deferred proposals") if held else ""
        return f"Nightly review {directory.name}: {launched}. Tell the user in one line.{earlier}{ask}"
    parts = [status(directory, mode) for mode in MODES]
    digest = "digest.md is ready" if (directory / "digest.md").exists() else "digest.md is not written yet"
    what = "it and the deferred proposals" if held else "it"
    return (f"Nightly review {directory.name} in {directory}: {'; '.join(parts)}; {digest}.{earlier} "
            + ASK.format(what=what))


def main() -> None:
    match sys.argv[1:]:
        case ["launch"]:
            for line in launch(date.today()):
                print(line)
        case ["offer"]:
            context = offer(datetime.now())
            if context:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                         "additionalContext": context}}))
        case _:
            sys.exit("usage: nightly_review.py launch|offer")


if __name__ == "__main__":
    main()
