"""Production fixtures backed by herdr's fake CLI and Linux process tree."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast, final
from unittest import mock

from scripts.production import unit_lookup
from scripts.herdr import herdr
from scripts.herdr.fake_herdr import CommandResponse, herdr_listener
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

PANE_RESULT = herdr.Herdr.result


@contextmanager
def private_process_registry() -> Generator[None]:
    """Read private process generations and retain normal reads for other processes."""
    from scripts.message import sessions
    from scripts.message import sessions as runtime_sessions

    original_start = runtime_sessions.process_start
    original_running = runtime_sessions.running_session

    def started(pid: int) -> str | None:
        root = os.environ.get("HERDR_PROC", "")
        path = Path(root) / str(pid) / "stat"
        if root and path.is_file() and not (path.parent / "kernel-process").exists():
            fields = path.read_text().rsplit(")", 1)[1].split()
            return fields[19] if len(fields) > 19 else None
        return original_start(pid)

    def running(record: runtime_sessions.SessionRecord) -> bool:
        root = os.environ.get("HERDR_PROC", "")
        directory = Path(root) / str(record["pid"])
        if root and (directory / "stat").is_file() and not (directory / "kernel-process").exists():
            return record.get("procStart", started(record["pid"])) == started(record["pid"])
        return original_running(record)

    with (mock.patch.object(sessions, "process_start", side_effect=started),
          mock.patch.object(runtime_sessions, "process_start", side_effect=started),
          mock.patch.object(sessions, "running_session", side_effect=running),
          mock.patch.object(runtime_sessions, "running_session", side_effect=running)):
        yield


def native_claude(environment: Mapping[str, str], pid: int, *, kernel_process: bool = False) -> None:
    """Publish the private process files needed for registry and liveness reads."""
    directory = Path(environment["HERDR_PROC"]) / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    if kernel_process:
        (directory / "kernel-process").touch()
    for name, content in (("environ", b""), ("cmdline", b"claude\0")):
        if not (directory / name).exists():
            _ = (directory / name).write_bytes(content)


def confirm_tab_close(responses: dict[str, CommandResponse | list[CommandResponse]],
                      session: str, tab: str, path: Path) -> None:
    """Remove the closed tab's panes from subsequent inventory responses."""
    command = f"--session {session} pane list"
    response = responses[command]
    assert not isinstance(response, list)
    result = herdr.json_object(cast(object, json.loads(response["stdout"])), "fixture response")
    inventory = herdr.json_object(result["result"], "fixture inventory")
    inventory["panes"] = [pane for pane in herdr.json_items(inventory["panes"], "fixture panes")
                          if herdr.json_object(pane, "fixture pane").get("tab_id") != tab]
    after = {**responses, command: CommandResponse(code=0, stdout=json.dumps(result))}
    _ = responses.setdefault(f"--session {session} tab close {tab}", CommandResponse(
        code=0, stdout="", write_files={str(path): json.dumps(after)}))


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


def labeled(session: str, pane: str, unit: str, production_name: str = "show", *,
            screen: str = "", agent_status: str = "idle", label: str = "", **environment: str) -> HostedUnit:
    """Publish a fixture member with its current native session identity."""
    from scripts.production.staff_test_support import unit_membership

    row = unit_membership(production_name, unit)
    return HostedUnit(session, pane, {"CODEX_THREAD_ID": row.current_session, **environment}, screen, agent_status, label)


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


def _write_children(root: Path) -> None:
    """Update thread child lists from the private process parent facts."""
    parents = {
        int(path.parent.name): int(path.read_text().rsplit(")", 1)[1].split()[1])
        for path in root.glob("[0-9]*/stat")
    }
    for pid in parents:
        task = root / str(pid) / "task" / str(pid)
        task.mkdir(parents=True, exist_ok=True)
        children = sorted(child for child, parent in parents.items() if parent == pid)
        _ = (task / "children").write_text(" ".join(str(child) for child in children))


def process(env: Mapping[str, str], pid: int, parent: int, argv: Sequence[str], marks: Mapping[str, str], *, started: str = "1",
            session: int | None = None, cwd: Path | None = None) -> None:
    directory = Path(env["HERDR_PROC"]) / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    _ = (directory / "stat").write_text(f"{pid} (fixture) S {parent} {pid} {session or pid} 0 {session or pid} " + "0 " * 13 + started)
    _ = (directory / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
    _ = (directory / "environ").write_bytes("\0".join(f"{key}={value}" for key, value in marks.items()).encode())
    link = directory / "cwd"
    link.unlink(missing_ok=True)
    link.symlink_to(cwd or directory.parent.parent)
    _ = (directory / "cgroup").write_text("")
    _write_children(directory.parent)


def write(env: Mapping[str, str], units: Sequence[HostedUnit]) -> None:
    """Publish fixture shells and session list/read responses using the real reader boundaries."""
    responses_file = Path(env["FAKE_HERDR_RESPONSES"])
    responses = cast(dict[str, CommandResponse], json.loads(responses_file.read_text()))
    sockets = Path(env["HERDR_PROC"]) / "net/unix"
    sockets.parent.mkdir(parents=True, exist_ok=True)
    _ = sockets.write_text("")
    by_session: dict[str, list[HostedUnit]] = {}
    for unit in units:
        by_session.setdefault(unit.session, []).append(unit)
    shells = {(unit.session, unit.pane): 991001 + index * 2 for index, unit in enumerate(units)}
    sessions: list[dict[str, object]] = []
    for name, panes in by_session.items():
        server = shells[(name, panes[0].pane)] - 1
        process(env, server, 0, ["herdr", "--session", name, "server"], {})
        herdr_listener(Path(env["HERDR_PROC"]), server, name, env)
        sessions.append({"name": name, "running": True, "default": False,
                         "session_dir": "/fixture", "socket_path": "/fixture/socket"})
        responses[f"--session {name} agent list"] = {"code": 0, "stdout": json.dumps({"result": {"agents": [
            {"pane_id": pane.pane, "agent": "claude", "agent_status": pane.agent_status} for pane in panes]}})}
        responses[f"--session {name} pane list"] = {"code": 0, "stdout": json.dumps({"result": {"panes": [
            {"pane_id": pane.pane, "label": pane.label or None} for pane in panes]}})}
        for unit in panes:
            shell = shells[(name, unit.pane)]
            process(env, shell, server, ["zsh"], {**unit.marks, "HERDR_PANE_ID": unit.pane, "HERDR_SESSION": name})
            responses[f"--session {name} pane process-info --pane {unit.pane}"] = {
                "code": 0, "stdout": json.dumps({"result": {"process_info": {
                    "shell_pid": shell, "foreground_process_group_id": shell,
                    "foreground_processes": [{"name": "zsh", "argv": ["zsh"], "pid": shell}]}}})}
            responses[f"--session {name} pane close {unit.pane}"] = {
                "code": 0, "stdout": "", "remove_paths": [str(Path(env["HERDR_PROC"]) / str(shell))]}
            for source in ("recent", "recent-unwrapped"):
                for lines in (3, 60, 400):
                    for ansi in ("", " --format ansi"):
                        responses[f"--session {name} pane read {unit.pane} --source {source} --lines {lines}{ansi}"] = {
                            "code": 0, "stdout": unit.screen}
    responses["--session default session list --json"] = {"code": 0, "stdout": json.dumps({"sessions": sessions})}
    before_close = {command: dict(response) for command, response in responses.items()}
    for unit in units:
        after_close = {**before_close, f"--session {unit.session} pane list": {
            "code": 0, "stdout": json.dumps({"result": {"panes": [
                {"pane_id": pane.pane, "label": pane.label or None}
                for pane in by_session[unit.session] if pane.pane != unit.pane]}})}}
        responses[f"--session {unit.session} pane close {unit.pane}"]["write_files"] = {
            str(responses_file): json.dumps(after_close)}
    _ = responses_file.write_text(json.dumps(responses))


def pane_foreground(env: Mapping[str, str], session: str, pane: str, shell: int, pid: int, command: str) -> None:
    """Update only the selected pane's foreground reply."""
    responses_file = Path(env["FAKE_HERDR_RESPONSES"])
    responses = cast(dict[str, CommandResponse], json.loads(responses_file.read_text()))
    responses[f"--session {session} pane process-info --pane {pane}"] = {
        "code": 0, "stdout": json.dumps({"result": {"process_info": {
            "shell_pid": shell, "foreground_process_group_id": pid,
            "foreground_processes": [{"name": command, "argv": [command], "pid": pid}]}}})}
    _ = responses_file.write_text(json.dumps(responses))


def place_process(env: Mapping[str, str], pid: int, pane: str, *, session: str = "", exec_shell: bool = False) -> None:
    """Put a registry process below the fixture shell owning this pane."""
    matches: list[tuple[Path, dict[str, str]]] = []
    for directory in Path(env["HERDR_PROC"]).iterdir():
        if not directory.name.isdigit():
            continue
        if (directory / "cmdline").read_bytes() != b"zsh\0":
            continue
        raw = (directory / "environ").read_bytes().decode().split("\0")
        marks = dict(item.split("=", 1) for item in raw if "=" in item)
        if marks.get("HERDR_PANE_ID") == pane and (not session or marks.get("HERDR_SESSION") == session):
            matches.append((directory, marks))
    if len(matches) != 1:
        raise AssertionError(f"expected one hosted pane {session}:{pane}, found {len(matches)}")
    directory, marks = matches[0]
    parent = int((directory / "stat").read_text().split()[3]) if exec_shell else int(directory.name)
    process(env, pid, parent, ["claude"], marks)
    pane_foreground(env, marks["HERDR_SESSION"], pane, pid if exec_shell else int(directory.name), pid, "claude")
    if exec_shell:
        # Generic launch fixtures use exec; pane-shell scenarios keep both processes.
        shutil.rmtree(directory)
        _write_children(directory.parent)


def calls(env: Mapping[str, str]) -> list[list[str]]:
    path = Path(env["FAKE_HERDR_LOG"])
    return [cast(list[str], json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []


def live_pid(test: unittest.TestCase) -> int:
    """A real live pid for registry checks, independent of the fake process contents."""
    child = subprocess.Popen(["sleep", "3600"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close() -> None:
        child.terminate()
        _ = child.wait(timeout=TEST_HANG_GUARD_SECS)

    test.addCleanup(close)
    return child.pid


@final
class PaneShellFixture:
    """One session-qualified pane host for lifecycle operations."""

    def __init__(self, case: unittest.TestCase, worktree: Path, process_root: Path, *,
                 live: bool, production_name: str = "demo", unit: str = "alpha",
                 env: dict[str, str] | None = None) -> None:
        from scripts.production import unit_move
        self.production_name: str = production_name
        self.unit: str = unit
        self.worktree: Path = worktree
        self.environment: dict[str, str] = {**(env or {}), **environment(process_root.parent)}
        self.environment["HERDR_PROC"] = str(process_root)
        if env is not None:
            env.update(self.environment)
            self.environment = env
        self.place: herdr.HerdrPlace = herdr.HerdrPlace("agents", "w1:p1")
        self.shell: int = 991001
        self.director: int = 4242
        from scripts.production import staff
        owner = staff.by_path(f"{production_name}/{unit}")
        assert owner is not None
        self.native_id: str = owner.current_session
        self.stop_calls: list[str] = []
        self.lifecycle: list[str] = []
        self._found: unit_lookup.LabeledSession | None = unit_lookup.LabeledSession(unit,
            unit_lookup.LiveClaude("alpha", self.native_id, "/fixture/socket", self.director)
            if live else unit_lookup.ClaudeNotRunning(), self.place,
            identity=unit_lookup.LiveSession(self.native_id))
        write(self.environment, [HostedUnit(self.place.session, self.place.pane,
              {"CLAUDE_CODE_SESSION_ID": self.native_id})])
        _ = case.enterContext(private_process_registry())
        if live:
            from scripts.production.staff_test_support import record_fixture_claude_start
            place_process(self.environment, self.director, self.place.pane)
            with mock.patch.dict(os.environ, {**self.environment, "FAKE_CLAUDE_PID": str(self.director),
                                             "FAKE_CLAUDE_SESSION_ID": self.native_id}):
                record_fixture_claude_start([])
        process(self.environment, self.shell, 991000, ["arbitrary-shell-name"],
                self.marks(), session=self.shell, cwd=worktree)
        self.parent(self.shell)
        if not live:
            shutil.rmtree(process_root / str(self.director))
            _write_children(process_root)
        case.enterContext(mock.patch.dict(os.environ, self.environment))
        _ = case.enterContext(mock.patch.object(herdr.Herdr, "result", PANE_RESULT))
        _ = case.enterContext(mock.patch.object(unit_move, "processes_inside", side_effect=self.inside_pids))
        _ = case.enterContext(mock.patch.object(unit_lookup, "labeled_session", side_effect=self.labeled_session))
        _ = case.enterContext(mock.patch.object(unit_lookup, "labeled_sessions", side_effect=self.labeled_sessions))

    def marks(self) -> dict[str, str]:
        return {"CLAUDE_CODE_SESSION_ID": self.native_id,
                "HERDR_PANE_ID": self.place.pane, "HERDR_SESSION": self.place.session}

    @property
    def found(self) -> unit_lookup.LabeledSession | None:
        return self._found if (Path(self.environment["HERDR_PROC"]) / str(self.shell)).exists() else None

    @found.setter
    def found(self, value: unit_lookup.LabeledSession | None) -> None:
        self._found = value

    def parent(self, pid: int) -> None:
        process(self.environment, self.director, pid, ["claude"], self.marks(),
                session=self.shell, cwd=self.worktree)

    def stopped(self) -> None:
        self.lifecycle.append("director exited")
        shutil.rmtree(Path(self.environment["HERDR_PROC"]) / str(self.director), ignore_errors=True)
        _write_children(Path(self.environment["HERDR_PROC"]))
        pane_foreground(self.environment, self.place.session, self.place.pane, self.shell, self.shell, "arbitrary-shell-name")
        self._found = unit_lookup.LabeledSession(self.unit, unit_lookup.ClaudeNotRunning(), self.place,
            identity=unit_lookup.LiveSession(self.native_id))

    def labeled_session(self, production_name: str, unit: str) -> unit_lookup.LabeledSession | None:
        return self.found if (production_name, unit) == (self.production_name, self.unit) else None

    def labeled_sessions(self, production_name: str) -> dict[str, unit_lookup.LabeledSession]:
        found = self.found
        return {self.unit: found} if production_name == self.production_name and found else {}

    def inside_pids(self, worktree: Path) -> list[int]:
        root = str(worktree)
        return sorted(int(path.parent.name) for path in Path(self.environment["HERDR_PROC"]).glob("[0-9]*/cwd")
                      if str(path.resolve()) == root or str(path.resolve()).startswith(root + "/"))

    def close_calls(self) -> list[list[str]]:
        return [call for call in calls(self.environment) if call[2:4] == ["pane", "close"]]

    def add_occupant(self, pid: int, *, unit: str = "", session: str = "agents",
                     marked: bool = True) -> None:
        """A visitor may reuse the pane id in another herdr session."""
        from scripts.production.staff_test_support import fixture_runtime_id
        identity = self.native_id if unit == self.unit else fixture_runtime_id("visitor:" + unit)
        marks = {**self.marks(), "CLAUDE_CODE_SESSION_ID": identity} if marked else {
            "HERDR_PANE_ID": self.place.pane, "HERDR_SESSION": session}
        parent = self.shell
        os_session = self.shell
        if session != self.place.session:
            server = pid + 1
            process(self.environment, server, 0, ["herdr", "--session", session, "server"], {})
            herdr_listener(Path(self.environment["HERDR_PROC"]), server, session, self.environment)
            responses_file = Path(self.environment["FAKE_HERDR_RESPONSES"])
            responses = cast(dict[str, CommandResponse], json.loads(responses_file.read_text()))
            responses[f"--session {session} pane list"] = responses[f"--session {self.place.session} pane list"]
            _ = responses_file.write_text(json.dumps(responses))
            parent, os_session = server, pid
        process(self.environment, pid, parent, ["visitor"], marks,
                session=os_session, cwd=self.worktree)
