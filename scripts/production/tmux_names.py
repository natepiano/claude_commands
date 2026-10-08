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
    session_id: str
    pane: str
    user_named: bool


class TmuxServerSocket(NamedTuple):
    path: Path


class TmuxServerUnavailable(NamedTuple):
    reason: str


class SessionRenamed(NamedTuple):
    descriptions: tuple[str, ...]


class RenameIncomplete(NamedTuple):
    reason: str


def _tmux_server_socket() -> TmuxServerSocket | TmuxServerUnavailable:
    try:
        result = subprocess.run([TMUX, "display-message", "-p", "#{socket_path}"],
                                capture_output=True, text=True, check=False)
    except OSError as error:
        return TmuxServerUnavailable(str(error))
    if result.returncode != 0:
        detail = result.stderr.strip() or f"tmux exited {result.returncode}"
        return TmuxServerUnavailable(detail)
    socket_path = result.stdout.strip()
    if not socket_path:
        return TmuxServerUnavailable("tmux returned no socket path")
    return TmuxServerSocket(Path(socket_path))


def live_sessions() -> list[ClaudeSession] | TmuxServerUnavailable:
    server = _tmux_server_socket()
    if isinstance(server, TmuxServerUnavailable):
        return server
    found: list[ClaudeSession] = []
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            pid = int(path.stem)
            os.kill(pid, 0)
            record = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
            name = record.get("name")
            session_id = record.get("sessionId")
            if not isinstance(name, str) or not name or not isinstance(session_id, str) or not session_id:
                continue
            environ = {
                key: value
                for entry in (PROC_DIR / str(pid) / "environ").read_bytes().decode(
                    errors="replace"
                ).split("\0")
                for key, separator, value in [entry.partition("=")]
                if separator
            }
            pane = environ.get("TMUX_PANE")
            session_tmux = environ.get("TMUX")
            if (pane and session_tmux
                    and Path(session_tmux.split(",", 1)[0]) == server.path):
                found.append(ClaudeSession(name, session_id, pane, record.get("nameSource") == "user"))
        except (OSError, ValueError, TypeError):
            continue
    return found


def _registry_holds(settings: showrunners.ShowrunnerSettings, name: str) -> bool:
    return any(runner["session"] == name or any(unit.session == name for unit in runner["units"])
               for runner in settings["showrunners"])


def _rename_session(pane: str, old: str, new: str, panes: dict[str, str]) -> SessionRenamed | RenameIncomplete:
    changed: list[str] = []
    try:
        settings = showrunners.load_settings() if showrunners.CONFIG.exists() else showrunners.defaults()
        if _registry_holds(settings, old):
            showrunners.change("rename", old, "", [], new)
            changed.append("showrunner registry")
    except (OSError, ValueError) as error:
        return RenameIncomplete(f"the showrunner registry still names {old}: {error}")

    current = panes.get(pane)
    if current == new:
        return SessionRenamed(tuple(changed))
    if current != old:
        shown = "no tmux session" if current is None else f"tmux session {current}"
        return RenameIncomplete(f"pane {pane} belongs to {shown}, not {old} or {new}")
    try:
        renamed = subprocess.run([TMUX, "rename-session", "-t", f"={old}", new],
                                 capture_output=True, text=True, check=False)
    except OSError as error:
        return RenameIncomplete(f"the tmux session still names {pane} {old}: {error}")
    if renamed.returncode != 0:
        detail = renamed.stderr.strip() or f"tmux exited {renamed.returncode}"
        return RenameIncomplete(f"the tmux session still names {pane} {old}: {detail}")
    changed.append("tmux session")
    return SessionRenamed(tuple(changed))


def rename_session(pane: str, old: str, new: str) -> SessionRenamed | RenameIncomplete:
    try:
        result = subprocess.run([TMUX, "list-panes", "-a", "-F", "#{session_name}\t#{pane_id}"],
                                capture_output=True, text=True, check=False)
    except OSError as error:
        return RenameIncomplete(f"the tmux session for pane {pane} could not be read: {error}")
    if result.returncode != 0:
        detail = result.stderr.strip() or f"tmux exited {result.returncode}"
        return RenameIncomplete(f"the tmux session for pane {pane} could not be read: {detail}")
    panes = {pane_id: name for line in result.stdout.splitlines() if "\t" in line
             for name, pane_id in [line.split("\t", 1)]}
    return _rename_session(pane, old, new, panes)


def fault(kind: str, old: str, new: str, settings: showrunners.ShowrunnerSettings) -> None:
    key = hashlib.sha256(f"{kind}\0{old}\0{new}".encode()).hexdigest()
    FAULT_STATE_DIR.mkdir(parents=True, exist_ok=True)
    marker = FAULT_STATE_DIR / key
    if marker.exists():
        return
    try:
        socket = showrunners.socket_for(settings["faults_to"])
    except OSError as error:
        print(f"tmux-names: {error}", file=sys.stderr)
        return
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
    if isinstance(sessions, TmuxServerUnavailable):
        return
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
        outcome = _rename_session(hosted[0].pane, old, new, panes)
        if isinstance(outcome, RenameIncomplete):
            print(f"tmux-names: {outcome.reason}", file=sys.stderr)
            continue
        names.remove(old)
        names.add(new)
        panes[hosted[0].pane] = new


def main() -> int:
    try:
        tick()
    except (OSError, ValueError) as error:
        print(f"tmux-names: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
