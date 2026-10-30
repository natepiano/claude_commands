"""Production fixtures backed by herdr's fake CLI and Linux process tree."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.lib import process_start as process_identity
from scripts.lib.process_start_test_support import write_stat
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "herdr"))
from fake_herdr import CommandResponse, herdr_listener


@dataclass(frozen=True)
class HostedUnit:
    session: str
    pane: str
    marks: dict[str, str]
    screen: str = ""
    # herdr's state for the Claude agent in the pane, as `agent list` reports it.
    agent_status: str = "idle"
    # The pane's label, as `pane list` reports it.
    label: str = ""


def environment(root: Path) -> dict[str, str]:
    """An isolated empty host; the caller may add units before invoking a CLI."""
    responses = root / "herdr-responses.json"
    if not responses.exists():
        _ = responses.write_text(json.dumps({"default": {"code": 0, "stdout": ""}, "--session default session list --json": {"code": 0, "stdout": '{"sessions": []}'}}))
    return {
        "HERDR_BIN": str(Path(__file__).resolve().parents[1] / "herdr/fake_herdr.py"),
        "HERDR_PROC": str(root / "proc"),
        "HERDR_PLATFORM": "linux",
        "FAKE_HERDR_LOG": str(root / "herdr.jsonl"),
        "FAKE_HERDR_RESPONSES": str(responses),
    }


def process(env: Mapping[str, str], pid: int, parent: int, argv: Sequence[str], marks: Mapping[str, str]) -> None:
    directory = Path(env["HERDR_PROC"]) / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    _ = write_stat(directory.parent, int(directory.name), ppid=parent)
    _ = (directory / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
    _ = (directory / "environ").write_bytes("\0".join(f"{key}={value}" for key, value in marks.items()).encode())
    cwd = directory / "cwd"
    if not cwd.is_symlink():
        cwd.symlink_to(directory.parent.parent)


def write(env: Mapping[str, str], units: Sequence[HostedUnit]) -> None:
    """Publish marked shells and session list/read responses using the real reader boundaries."""
    responses_file = Path(env["FAKE_HERDR_RESPONSES"])
    responses = cast(dict[str, CommandResponse], json.loads(responses_file.read_text()))
    sockets = Path(env["HERDR_PROC"]) / "net/unix"
    sockets.parent.mkdir(parents=True, exist_ok=True)
    _ = sockets.write_text("")
    sessions: list[dict[str, object]] = []
    for index, unit in enumerate(units):
        server = 991000 + index * 2
        process(env, server, 0, ["herdr", "--session", unit.session, "server"], {})
        herdr_listener(Path(env["HERDR_PROC"]), server, unit.session, env)
        process(env, server + 1, server, ["zsh"], {**unit.marks, "HERDR_PANE_ID": unit.pane})
        sessions.append({"name": unit.session, "running": True, "default": False,
                         "session_dir": "/fixture", "socket_path": "/fixture/socket"})
        responses[f"--session {unit.session} agent list"] = {"code": 0, "stdout": json.dumps({"result": {"agents": [
            {"pane_id": unit.pane, "agent": "claude", "agent_status": unit.agent_status}]}})}
        responses[f"--session {unit.session} pane list"] = {"code": 0, "stdout": json.dumps({"result": {"panes": [
            {"pane_id": unit.pane, "label": unit.label or None}]}})}
        for source in ("recent", "recent-unwrapped"):
            for lines in (3, 60, 400):
                for ansi in ("", " --format ansi"):
                    responses[f"--session {unit.session} pane read {unit.pane} --source {source} --lines {lines}{ansi}"] = {
                        "code": 0, "stdout": unit.screen}
    responses["--session default session list --json"] = {"code": 0, "stdout": json.dumps({"sessions": sessions})}
    _ = responses_file.write_text(json.dumps(responses))


def place_process(env: Mapping[str, str], pid: int, pane: str) -> None:
    """Put a registry process below the fixture shell owning this pane."""
    for directory in Path(env["HERDR_PROC"]).iterdir():
        if not directory.name.isdigit():
            continue
        if (directory / "cmdline").read_bytes() != b"zsh\0":
            continue
        raw = (directory / "environ").read_bytes().decode().split("\0")
        marks = dict(item.split("=", 1) for item in raw if "=" in item)
        if marks.get("HERDR_PANE_ID") == pane:
            stat = process_identity.read_stat(int(directory.name), directory.parent)
            if isinstance(stat, process_identity.ProcessGone):
                continue
            parent = stat.ppid
            process(env, pid, parent, ["claude"], marks)
            return
    raise AssertionError(f"no hosted pane {pane}")


def calls(env: Mapping[str, str]) -> list[list[str]]:
    path = Path(env["FAKE_HERDR_LOG"])
    return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []


def live_pid(test: unittest.TestCase) -> int:
    """A real live pid for registry checks, independent of the fake process contents."""
    child = subprocess.Popen(["sleep", "3600"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close() -> None:
        child.terminate()
        _ = child.wait(timeout=30)

    test.addCleanup(close)
    return child.pid
