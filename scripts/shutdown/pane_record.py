"""Each herdr pane's Claude launch, so a restore can resume the pane on its own account.

One file per herdr session, `~/.local/state/herdr-panes/<session>.json`, maps HERDR_PANE_ID to the
launch of the Claude in that pane: its account folder, its cwd, its name and model flags and the
environment marks it started with. The SessionStart hook (`session-start-pane-record.py`) writes
the entry from its own Claude process at every start, resume, clear and compaction, so a pane
started by hand is covered the same as one restart or add_unit started (user, 2026-10-19).

The conversation id is herdr's: its own hook keeps `agent_session` for each pane. The id here is
the one the pane had at its last start, a check on herdr's, never a second source.

A pane herdr no longer has is ignored, so a stale entry needs no cleanup. A session that ends
other than by a signal (`/exit`, logout) marks its entry ended (`session-end-pane-record.py`), so a
restore leaves a Claude closed by hand closed.

`seed` writes the entries for the Claudes already running, read from herdr's process info.

Linux reads a process from /proc. The Mac has no /proc: it reads the parent with `ps` and the
arguments and environment from the kernel's KERN_PROCARGS2 record, split by
`account.procargs2_parts`. Of an environment, only the names below are kept; every other value is
dropped as it is read and never written or printed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict, cast

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS.parent))
from scripts.lib import process_start as process_identity
sys.path.insert(0, str(SCRIPTS / "herdr"))
sys.path.insert(0, str(SCRIPTS / "lib"))
sys.path.insert(0, str(SCRIPTS / "whoami"))
import herdr  # noqa: E402
from probe_session import PROBE_ENV  # noqa: E402

RECORD_DIR = Path(os.environ.get("HERDR_PANE_RECORD_DIR") or Path.home() / ".local/state/herdr-panes")
# The launch environment a resume must give back. Credentials never enter the record.
KEPT_NAMES = frozenset({"ENABLE_TOOL_SEARCH", PROBE_ENV})
KEPT_PREFIXES = ("SHOWRUNNER_",)
# Claude flags whose value the resume repeats; every other argument stays out of the record.
NAME_FLAGS = frozenset({"-n", "--name"})
MODEL_FLAGS = frozenset({"--model"})
ANCESTOR_LIMIT = 16
PROC = Path(os.environ.get("HERDR_PROC", "/proc"))


class PaneLaunch(TypedDict):
    pane_id: str
    session_id: str
    config_dir: str
    cwd: str
    name: str
    model: str
    remote_control: bool
    environment: dict[str, str]
    # False when the launch flags could not be read: a restore refuses it.
    flags_read: bool
    ended: bool
    written_at: str


def record_path(session: str) -> Path:
    return RECORD_DIR / f"{session}.json"


def _read_entries(session: str) -> dict[str, object]:
    path = record_path(session)
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path}: pane records must be an object")
    return cast(dict[str, object], value)


def read_strict(session: str) -> dict[str, PaneLaunch]:
    """Read validated launch evidence; only an absent file means there are no entries."""
    entries = _read_entries(session)
    for pane_id, value in entries.items():
        location = f"{record_path(session)}: pane {pane_id}"
        if not isinstance(value, dict):
            raise ValueError(f"{location}: launch must be an object")
        launch = cast(dict[str, object], value)
        environment = launch.get("environment")
        if (not isinstance(launch.get("session_id"), str)
                or not isinstance(launch.get("written_at"), str)
                or not isinstance(environment, dict)
                or not all(isinstance(name, str) and isinstance(setting, str)
                           for name, setting in cast(dict[object, object], environment).items())):
            raise ValueError(f"{location}: expected session_id and written_at strings and a string environment")
    return cast(dict[str, PaneLaunch], entries)


def read(session: str) -> dict[str, PaneLaunch]:
    """The session's entries by pane id; an absent or unreadable file reads as none."""
    try:
        return cast(dict[str, PaneLaunch], _read_entries(session))
    except (OSError, ValueError):
        return {}


@contextmanager
def _locked(session: str) -> Generator[None]:
    RECORD_DIR.mkdir(parents=True, exist_ok=True)
    with open(RECORD_DIR / f".{session}.lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _save(session: str, entries: dict[str, PaneLaunch]) -> None:
    handle, temporary = tempfile.mkstemp(dir=RECORD_DIR, prefix=f".{session}.", suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        json.dump(entries, file, indent=2, sort_keys=True)
        _ = file.write("\n")
    os.replace(temporary, record_path(session))


def write(session: str, launch: PaneLaunch) -> None:
    """Replace the pane's entry and keep every other pane's."""
    with _locked(session):
        entries = read(session)
        entries[launch["pane_id"]] = launch
        _save(session, entries)


def end(session: str, pane_id: str, session_id: str) -> None:
    """Mark the pane's entry ended when it is still this conversation's."""
    with _locked(session):
        entries = read(session)
        entry = entries.get(pane_id)
        if entry is None or entry["session_id"] != session_id:
            return
        entry["ended"] = True
        _save(session, entries)


def forget_unit(unit_id: str) -> list[str]:
    """Drop every entry of a unit's director, in each session's record; a rename relaunches it in a new pane."""
    dropped: list[str] = []
    for path in sorted(RECORD_DIR.glob("*.json")):
        with _locked(path.stem):
            entries = read(path.stem)
            kept = {pane: entry for pane, entry in entries.items()
                    if entry["environment"].get("SHOWRUNNER_UNIT_ID") != unit_id}
            if len(kept) != len(entries):
                _save(path.stem, kept)
                dropped.extend(f"{path.stem}:{pane}" for pane in entries if pane not in kept)
    return dropped


def _needed(name: str) -> bool:
    """An environment name a record reads; the value of any other name is dropped as it is read."""
    return name == "CLAUDE_CONFIG_DIR" or name in KEPT_NAMES or name.startswith(KEPT_PREFIXES)


def _environment(parts: Sequence[bytes]) -> dict[str, str]:
    pairs = (os.fsdecode(part).partition("=") for part in parts if b"=" in part)
    return {key: value for key, _, value in pairs if _needed(key)}


def _mac_record(pid: int) -> tuple[list[str], dict[str, str]]:
    # Imported here: the Linux hook never needs the account module.
    from account import kern_procargs2, procargs2_parts

    parts = procargs2_parts(kern_procargs2(pid), _needed)
    return list(parts.arguments), parts.environment


def process_parent(pid: int) -> int:
    """Read the OS parent; callers decide how unavailable evidence is handled."""
    if (os.environ.get("HERDR_PLATFORM") or sys.platform) == "darwin":
        result = subprocess.run([os.environ.get("HERDR_PS", "ps"), "-o", "ppid=", "-p", str(pid)],
                                capture_output=True, text=True, check=True, timeout=5)
        return int(result.stdout)
    stat = process_identity.read_stat(pid, Path(os.environ.get("HERDR_PROC") or PROC))
    if isinstance(stat, process_identity.ProcessGone):
        raise FileNotFoundError(f"process {pid} is gone")
    return stat.ppid


def pane_shell_pid(place: herdr.HerdrPlace) -> int:
    info = herdr.json_object(herdr.Herdr(place.session).result(
        "pane", "process-info", "--pane", place.pane).get("process_info"), "pane process info")
    shell = info.get("shell_pid")
    if isinstance(shell, bool) or not isinstance(shell, int) or shell <= 0:
        raise ValueError("pane process info: expected a positive shell_pid")
    return shell


def _arguments(pid: int) -> list[str]:
    if sys.platform == "darwin":
        return _mac_record(pid)[0]
    raw = (PROC / str(pid) / "cmdline").read_bytes()
    return [os.fsdecode(part) for part in raw.split(b"\0") if part]


def claude_arguments(pid: int) -> list[str]:
    """The argv of the nearest Claude process above `pid`; empty when none is found."""
    for _ in range(ANCESTOR_LIMIT):
        try:
            pid = process_parent(pid)
            arguments = _arguments(pid)
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return []
        if herdr.is_claude_process(arguments):
            return arguments
        if pid <= 1:
            return []
    return []


def _flag_value(arguments: Sequence[str], flags: frozenset[str]) -> str:
    value = ""
    for index, argument in enumerate(arguments):
        name, equals, inline = argument.partition("=")
        if name in flags:
            value = inline if equals else (arguments[index + 1] if index + 1 < len(arguments) else "")
    return value


def launch(pane_id: str, session_id: str, cwd: str, environment: Mapping[str, str],
           arguments: Sequence[str]) -> PaneLaunch:
    config_dir = environment.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return {
        "pane_id": pane_id,
        "session_id": session_id,
        "config_dir": config_dir,
        "cwd": cwd,
        "name": _flag_value(arguments, NAME_FLAGS),
        "model": _flag_value(arguments, MODEL_FLAGS),
        "remote_control": any(argument.partition("=")[0] == "--remote-control" for argument in arguments),
        "environment": {key: value for key, value in sorted(environment.items())
                        if key in KEPT_NAMES or key.startswith(KEPT_PREFIXES)},
        "flags_read": bool(arguments),
        "ended": False,
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _process_environment(pid: int) -> dict[str, str]:
    """The needed part of `pid`'s environment."""
    if sys.platform == "darwin":
        return _mac_record(pid)[1]
    return _environment((PROC / str(pid) / "environ").read_bytes().split(b"\0"))


def _conversation_of(config_dir: str, pid: int) -> str:
    """The conversation id Claude keeps for its own process, `<config>/sessions/<pid>.json`."""
    try:
        value = cast(object, json.loads((Path(config_dir) / "sessions" / f"{pid}.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return ""
    session_id = cast(dict[str, object], value).get("sessionId") if isinstance(value, dict) else None
    return session_id if isinstance(session_id, str) else ""


def seed(session: str) -> int:
    """Write the entry of each pane whose foreground runs Claude; name each pane it cannot read."""
    client = herdr.Herdr(session)
    for pane in client.panes():
        pane_id = pane["pane_id"]
        context = f"herdr {session}:{pane_id} process info"
        info = herdr.json_object(client.result("pane", "process-info", "--pane", pane_id).get("process_info"), context)
        processes = [herdr.json_object(item, context) for item in herdr.json_items(info.get("foreground_processes"), context)]
        found = [item for item in processes
                 if isinstance(argv := item.get("argv"), list)
                 and herdr.is_claude_process([str(part) for part in cast(list[object], argv)])]
        if not found:
            continue
        pid = found[0].get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int):
            print(f"{pane_id}: no Claude process id")
            continue
        arguments = [str(part) for part in cast(list[object], found[0].get("argv"))]
        try:
            environment = _process_environment(pid)
        except (OSError, ValueError) as error:
            print(f"{pane_id}: cannot read the environment of {pid}: {error}")
            continue
        config_dir = environment.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
        session_id = _conversation_of(config_dir, pid)
        if not session_id:
            print(f"{pane_id}: no conversation id for {pid}")
            continue
        cwd = found[0].get("cwd")
        write(session, launch(pane_id, session_id, cwd if isinstance(cwd, str) else "", environment, arguments))
        print(f"{pane_id}: {pane.get('label') or ''} {session_id}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Write the pane launch entries of the Claudes running now.")
    _ = parser.add_argument("command", choices=["seed"])
    _ = parser.add_argument("--session", default="")
    options = parser.parse_args()
    sys.exit(seed(cast(str, options.session) or herdr.shared_session() or "default"))
