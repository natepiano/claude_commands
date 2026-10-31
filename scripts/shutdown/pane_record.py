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
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import cast

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS / "herdr"))
sys.path.insert(0, str(SCRIPTS / "lib"))
sys.path.insert(0, str(SCRIPTS / "whoami"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import herdr  # noqa: E402
import pane_launch
import sessions
from pane_launch import PaneLaunch as PaneLaunch
from probe_session import PROBE_ENV  # noqa: E402

RECORD_DIR = pane_launch.record_directory()
# The launch environment a resume must give back. Credentials never enter the record.
KEPT_NAMES = frozenset({"ENABLE_TOOL_SEARCH", "STAFF_ID", PROBE_ENV})
KEPT_PREFIXES = ("SHOWRUNNER_",)
# Claude flags whose value the resume repeats; every other argument stays out of the record.
NAME_FLAGS = frozenset({"-n", "--name"})
MODEL_FLAGS = frozenset({"--model"})
ANCESTOR_LIMIT = 16
PROC = Path(os.environ.get("HERDR_PROC", "/proc"))


def record_directory() -> Path:
    """Use the configured store, including an explicit binding by an embedded caller."""
    return RECORD_DIR


def record_path(session: str) -> Path:
    return record_directory() / f"{session}.json"


def _read_entries(session: str, *, dependencies: Callable[[Path], None] | None = None) -> dict[str, object]:
    path = record_path(session)
    if dependencies is not None:
        dependencies(path)
    return pane_launch.read_entries(path)


def read_strict(session: str, *, dependencies: Callable[[Path], None] | None = None) -> dict[str, PaneLaunch]:
    """Read validated launch evidence; only an absent file means there are no entries."""
    entries = _read_entries(session, dependencies=dependencies)
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
    return pane_launch.read(record_path(session))


def launches() -> list[PaneLaunch]:
    """Read retained launches through the same per-session reader as restore."""
    return [entry for path in record_directory().glob("*.json") for entry in read(path.stem).values()]


def launch_for_session(session_id: str) -> PaneLaunch | None:
    """Read a conversation's newest launch, including account moves and intentional exits."""
    matches = [entry for entry in launches() if entry["session_id"] == session_id]
    if not matches:
        return None
    return max(matches, key=lambda entry: entry["written_at"])


@contextmanager
def locked(session: str) -> Generator[None]:
    record_directory().mkdir(parents=True, exist_ok=True)
    with open(record_directory() / f".{session}.lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save(session: str, entries: dict[str, PaneLaunch]) -> None:
    handle, temporary = tempfile.mkstemp(dir=record_directory(), prefix=f".{session}.", suffix=".tmp")
    with os.fdopen(handle, "w", encoding="utf-8") as file:
        json.dump(entries, file, indent=2, sort_keys=True)
        _ = file.write("\n")
    os.replace(temporary, record_path(session))


def write(session: str, launch: PaneLaunch) -> None:
    """Replace the pane's entry and keep every other pane's."""
    with locked(session):
        entries = read(session)
        launch = launch.copy()
        launch["environment"] = retained_environment(launch, tuple(entries.values()))
        if entries.get(launch["pane_id"]) == launch:
            return
        entries[launch["pane_id"]] = launch
        save(session, entries)


def retained_environment(launch: PaneLaunch, history: Sequence[PaneLaunch]) -> dict[str, str]:
    """Keep a conversation's identity when its terminal command lost the environment."""
    environment: dict[str, str] = {}
    for previous in sorted(history, key=lambda item: item["written_at"]):
        if previous["session_id"] == launch["session_id"]:
            environment.update({key: value for key, value in previous["environment"].items()
                                if (key in herdr.MEMBER_MARKS or key.startswith(KEPT_PREFIXES))
                                and not (launch["environment"].get("STAFF_ID") and key in herdr.MEMBER_MARKS)})
    environment.update(launch["environment"])
    return environment


def matching_launch(place: herdr.HerdrPlace, session_id: str) -> PaneLaunch | None:
    """A pane's retained launch is evidence only for that exact conversation."""
    return pane_launch.matching_launch(read(place.session), place, session_id)


def end(session: str, pane_id: str, session_id: str) -> None:
    """Mark the pane's entry ended when it is still this conversation's."""
    with locked(session):
        entries = read(session)
        entry = entries.get(pane_id)
        if entry is None or entry["session_id"] != session_id:
            return
        entry["ended"] = True
        save(session, entries)


def forget_unit(unit_id: str) -> list[str]:
    """Drop every entry of a unit's director, in each session's record; a rename relaunches it in a new pane."""
    dropped: list[str] = []
    for path in sorted(record_directory().glob("*.json")):
        with locked(path.stem):
            entries = read(path.stem)
            kept = {pane: entry for pane, entry in entries.items()
                    if entry["environment"].get("SHOWRUNNER_UNIT_ID") != unit_id}
            if len(kept) != len(entries):
                save(path.stem, kept)
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
    from scripts.whoami.account import kern_procargs2, procargs2_parts

    parts = procargs2_parts(kern_procargs2(pid), _needed)
    return list(parts.arguments), parts.environment


def _process_stat(process: Path) -> list[str]:
    return (process / "stat").read_text().rsplit(")", 1)[1].split()


def process_parent(pid: int) -> int:
    """Read the OS parent; callers decide how unavailable evidence is handled."""
    if (os.environ.get("HERDR_PLATFORM") or sys.platform) == "darwin":
        result = subprocess.run([os.environ.get("HERDR_PS", "ps"), "-o", "ppid=", "-p", str(pid)],
                                capture_output=True, text=True, check=True, timeout=5)
        return int(result.stdout)
    return int(_process_stat(Path(os.environ.get("HERDR_PROC") or PROC) / str(pid))[1])


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


@dataclass(frozen=True)
class ClaudeArguments:
    values: dict[str, str]
    has_prompt: bool

    def value(self, flags: frozenset[str]) -> str:
        return next((value for name, value in reversed(tuple(self.values.items())) if name in flags), "")


# Claude options used by launch commands; all consumers share this parse.
CLAUDE_VALUE_FLAGS = frozenset({"--resume", "-r", "--remote-control", "--settings",
    "--model", "--name", "-n", "--permission-mode", "--session-id"})
RESUME_FLAGS = frozenset({"--resume", "-r"})


def parse_claude_arguments(arguments: Sequence[str]) -> ClaudeArguments:
    values: dict[str, str] = {}
    prompt = False
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        index += 1
        option, equals, value = argument.partition("=")
        if option in CLAUDE_VALUE_FLAGS:
            if not equals and index < len(arguments) and not arguments[index].startswith("-"):
                value = arguments[index]
                index += 1
            _ = values.pop(option, None)
            values[option] = value
        elif not argument.startswith("-") or argument == "--":
            prompt = True
    return ClaudeArguments(values, prompt)


def launch(pane_id: str, session_id: str, cwd: str, environment: Mapping[str, str],
           arguments: Sequence[str]) -> PaneLaunch:
    config_dir = environment.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    parsed = parse_claude_arguments(arguments)
    return {
        "pane_id": pane_id,
        "session_id": session_id,
        "config_dir": config_dir,
        "cwd": cwd,
        "name": parsed.value(NAME_FLAGS),
        "model": parsed.value(MODEL_FLAGS),
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


def running_launch(record: sessions.SessionRecord, place: herdr.HerdrPlace, cwd: str) -> PaneLaunch:
    """Capture one verified live Claude using the existing argv and environment readers."""
    if not sessions.running_session(record):
        raise ValueError(f"{record['sessionId']}: Claude is no longer running")
    environment = _process_environment(record["pid"])
    arguments = _arguments(record["pid"])
    if not herdr.is_claude_process(arguments):
        raise ValueError(f"{record['sessionId']}: process is not Claude")
    entry = launch(place.pane, record["sessionId"], cwd, environment, arguments)
    entry["name"] = record["name"]
    return entry


def _conversation_of(config_dir: str, pid: int) -> str:
    """The conversation id Claude keeps for its own process, `<config>/sessions/<pid>.json`."""
    try:
        value = cast(object, json.loads((Path(config_dir) / "sessions" / f"{pid}.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return ""
    session_id = cast(dict[str, object], value).get("sessionId") if isinstance(value, dict) else None
    return session_id if isinstance(session_id, str) else ""


def seed(session: str, *, select: Callable[[herdr.PaneIdentity], bool] | None = None) -> int:
    """Write the entry of each pane whose foreground runs Claude; name each pane it cannot read."""
    client = herdr.Herdr(session)
    for pane in client.panes():
        pane_id = pane["pane_id"]
        if select is not None and not select(pane):
            continue
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
