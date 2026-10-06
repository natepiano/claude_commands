#!/usr/bin/env python3
"""Make tmux session names follow live Claude session names."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, cast

import showrunners

SESSIONS_DIR = Path(os.environ.get("NOTIFIER_SESSIONS_DIR") or Path.home() / ".claude/sessions")
PROC_DIR = Path(os.environ.get("TMUX_NAMES_PROC_DIR") or "/proc")
TMUX = os.environ.get("TMUX_NAMES_TMUX") or "tmux"
SEND = Path(os.environ.get("TMUX_NAMES_SEND") or Path(__file__).resolve().parent.parent / "message/send.py")
FAULT_STATE_DIR = Path(os.environ.get("TMUX_NAMES_FAULT_STATE_DIR") or Path.home() / ".local/state/tmux-names")


class ClaudeSession(NamedTuple):
    name: str
    pane: str
    user_named: bool


def live_sessions() -> list[ClaudeSession]:
    found: list[ClaudeSession] = []
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            pid = int(path.stem)
            os.kill(pid, 0)
            record = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
            name = record.get("name")
            if not isinstance(name, str) or not name:
                continue
            environ = (PROC_DIR / str(pid) / "environ").read_bytes()
            pane = next((entry.removeprefix("TMUX_PANE=") for entry in environ.decode(errors="replace").split("\0")
                         if entry.startswith("TMUX_PANE=")), "")
            if pane:
                found.append(ClaudeSession(name, pane, record.get("nameSource") == "user"))
        except (OSError, ValueError, TypeError):
            continue
    return found


def fault(kind: str, old: str, new: str, settings: showrunners.ShowrunnerSettings) -> None:
    key = hashlib.sha256(f"{kind}\0{old}\0{new}".encode()).hexdigest()
    FAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    marker = FAULT_STATE_DIR / key
    if marker.exists():
        return
    socket = showrunners.socket_for(settings["faults_to"])
    if socket is None:
        return
    message = f"tmux-names: skipped {old} → {new}: {kind}"
    result = subprocess.run([sys.executable, str(SEND), "--to", f"uds:{socket}", "--from", "tmux-names",
                             "--key", f"tmux-names:{key}", "--text", message],
                            capture_output=True, text=True, check=False)
    if result.returncode == 0:
        marker.touch()
    else:
        print(f"tmux-names: fault delivery failed: {result.stderr.strip()}", file=sys.stderr)


def tick() -> None:
    try:
        result = subprocess.run([TMUX, "list-panes", "-a", "-F", "#{session_name}\t#{pane_id}"],
                                capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return
    if result.returncode != 0:
        return
    panes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "\t" in line:
            name, pane = line.split("\t", 1)
            panes[pane] = name
    if not panes:
        return
    sessions = live_sessions()
    by_tmux: dict[str, list[ClaudeSession]] = {}
    for session in sessions:
        old = panes.get(session.pane)
        if old is not None:
            by_tmux.setdefault(old, []).append(session)
    settings = showrunners.load_settings() if showrunners.CONFIG.exists() else showrunners.defaults()
    names = set(panes.values())
    for old, hosted in by_tmux.items():
        new = hosted[0].name
        if len(hosted) > 1:
            fault("more than one Claude session", old, new, settings)
            continue
        if not hosted[0].user_named:
            continue
        if old == new:
            continue
        if new in names:
            fault("name already taken", old, new, settings)
            continue
        renamed = subprocess.run([TMUX, "rename-session", "-t", f"={old}", new],
                                 capture_output=True, text=True, check=False)
        if renamed.returncode != 0:
            print(f"tmux-names: rename {old} → {new}: {renamed.stderr.strip()}", file=sys.stderr)
            continue
        try:
            showrunners.change("rename", old, "", [], new)
        except (OSError, ValueError) as error:
            print(f"tmux-names: config rename {old} → {new}: {error}", file=sys.stderr)
        names.remove(old)
        names.add(new)


def main() -> int:
    try:
        tick()
    except (OSError, ValueError) as error:
        print(f"tmux-names: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
