"""Production fixtures backed by herdr's fake CLI and Linux process tree."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast, final
from unittest import mock

if TYPE_CHECKING:
    import herdr
    import unit_lookup
    import shutdown_record

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "herdr"))
import herdr
from fake_herdr import CommandResponse, herdr_listener

PANE_RESULT = herdr.Herdr.result
TEST_HANG_GUARD_SECS = 300  # load 136 on natedev, 2026-10-29


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


def process(env: Mapping[str, str], pid: int, parent: int, argv: Sequence[str], marks: Mapping[str, str], *, started: str = "1",
            session: int | None = None, cwd: Path | None = None) -> None:
    directory = Path(env["HERDR_PROC"]) / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    _ = (directory / "stat").write_text(f"{pid} (fixture) S {parent} {pid} {session or pid} {session or pid} 0 " + "0 " * 13 + started)
    _ = (directory / "cmdline").write_bytes(("\0".join(argv) + "\0").encode())
    _ = (directory / "environ").write_bytes("\0".join(f"{key}={value}" for key, value in marks.items()).encode())
    link = directory / "cwd"
    link.unlink(missing_ok=True)
    link.symlink_to(cwd or directory.parent.parent)
    _ = (directory / "cgroup").write_text("")


def write(env: Mapping[str, str], units: Sequence[HostedUnit]) -> None:
    """Publish marked shells and session list/read responses using the real reader boundaries."""
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
                "code": 0, "stdout": json.dumps({"result": {"process_info": {"shell_pid": shell}}})}
            responses[f"--session {name} pane close {unit.pane}"] = {
                "code": 0, "stdout": "", "remove_paths": [str(Path(env["HERDR_PROC"]) / str(shell))]}
            for source in ("recent", "recent-unwrapped"):
                for lines in (3, 60, 400):
                    for ansi in ("", " --format ansi"):
                        responses[f"--session {name} pane read {unit.pane} --source {source} --lines {lines}{ansi}"] = {
                            "code": 0, "stdout": unit.screen}
    responses["--session default session list --json"] = {"code": 0, "stdout": json.dumps({"sessions": sessions})}
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
    parent = int((directory / "stat").read_text().split()[3])
    process(env, pid, parent, ["claude"], marks)
    if exec_shell:
        # Generic launch fixtures use exec; pane-shell scenarios keep both processes.
        responses_file = Path(env["FAKE_HERDR_RESPONSES"])
        responses = cast(dict[str, CommandResponse], json.loads(responses_file.read_text()))
        responses[f"--session {marks['HERDR_SESSION']} pane process-info --pane {pane}"] = {
            "code": 0, "stdout": json.dumps({"result": {"process_info": {"shell_pid": pid}}})}
        _ = responses_file.write_text(json.dumps(responses))
        shutil.rmtree(directory)


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


@final
class PaneShellFixture:
    """One session-qualified pane host for lifecycle operations."""

    def __init__(self, case: unittest.TestCase, worktree: Path, process_root: Path, *,
                 live: bool, slug: str = "demo", unit: str = "alpha-unit",
                 env: dict[str, str] | None = None) -> None:
        import herdr
        import unit_lookup
        import unit_move
        self.slug: str = slug
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
        self.stop_calls: list[str] = []
        self.lifecycle: list[str] = []
        self._found: unit_lookup.MarkedUnit | None = unit_lookup.MarkedUnit(unit,
            unit_lookup.LiveClaude("alpha", "sid-alpha", "/fixture/socket", self.director)
            if live else unit_lookup.ClaudeNotRunning(), self.place)
        write(self.environment, [HostedUnit(self.place.session, self.place.pane,
              {unit_lookup.PRODUCTION_MARK: slug, unit_lookup.UNIT_MARK: unit})])
        process(self.environment, self.shell, 991000, ["arbitrary-shell-name"],
                self.marks(), session=self.shell, cwd=worktree)
        self.parent(self.shell)
        if not live:
            shutil.rmtree(process_root / str(self.director))
        case.enterContext(mock.patch.dict(os.environ, self.environment))
        _ = case.enterContext(mock.patch.object(herdr.Herdr, "result", PANE_RESULT))
        _ = case.enterContext(mock.patch.object(unit_move, "processes_inside", side_effect=self.inside_pids))
        _ = case.enterContext(mock.patch.object(unit_lookup, "marked_unit", side_effect=self.marked_unit))
        _ = case.enterContext(mock.patch.object(unit_lookup, "marked_units", side_effect=self.marked_units))

    def marks(self) -> dict[str, str]:
        return {"SHOWRUNNER_UNIT": self.slug, "SHOWRUNNER_UNIT_ID": self.unit,
                "HERDR_PANE_ID": self.place.pane, "HERDR_SESSION": self.place.session}

    @property
    def found(self) -> unit_lookup.MarkedUnit | None:
        return self._found if (Path(self.environment["HERDR_PROC"]) / str(self.shell)).exists() else None

    @found.setter
    def found(self, value: unit_lookup.MarkedUnit | None) -> None:
        self._found = value

    def parent(self, pid: int) -> None:
        process(self.environment, self.director, pid, ["claude"], self.marks(),
                session=self.shell, cwd=self.worktree)

    def stopped(self) -> None:
        import unit_lookup
        self.lifecycle.append("director exited")
        shutil.rmtree(Path(self.environment["HERDR_PROC"]) / str(self.director), ignore_errors=True)
        self._found = unit_lookup.MarkedUnit(self.unit, unit_lookup.ClaudeNotRunning(), self.place)

    def marked_unit(self, slug: str, unit: str) -> unit_lookup.MarkedUnit | None:
        return self.found if (slug, unit) == (self.slug, self.unit) else None

    def marked_units(self, slug: str) -> dict[str, unit_lookup.MarkedUnit]:
        found = self.found
        return {self.unit: found} if slug == self.slug and found else {}

    def inside_pids(self, worktree: Path) -> list[int]:
        root = str(worktree)
        return sorted(int(path.parent.name) for path in Path(self.environment["HERDR_PROC"]).glob("[0-9]*/cwd")
                      if str(path.resolve()) == root or str(path.resolve()).startswith(root + "/"))

    def close_calls(self) -> list[list[str]]:
        return [call for call in calls(self.environment) if call[2:4] == ["pane", "close"]]

    def add_occupant(self, pid: int, *, unit: str = "", session: str = "agents",
                     marked: bool = True) -> None:
        """A visitor may reuse the pane id in another herdr session."""
        marks = {**self.marks(), "SHOWRUNNER_UNIT_ID": unit} if marked else {
            "HERDR_PANE_ID": self.place.pane, "HERDR_SESSION": session}
        parent = self.shell
        os_session = self.shell
        if session != self.place.session:
            server = pid + 1
            process(self.environment, server, 0, ["herdr", "--session", session, "server"], {})
            herdr_listener(Path(self.environment["HERDR_PROC"]), server, session, self.environment)
            parent, os_session = server, pid
        process(self.environment, pid, parent, ["visitor"], marks,
                session=os_session, cwd=self.worktree)


def stopped_record(root: Path, worktree: Path, *, name: str = "alpha", cwd: Path | None = None,
                   production: str = "demo", unit: str = "alpha-unit", doc: Path | None = None,
                   host_session: str = "agents", branch: str = "", plan: Path | None = None,
                   progress: Literal["waiting", "ready", "stopped"] = "stopped") -> shutdown_record.SessionShutdown:
    """One typed unit record builder; callers override only their scenario's values."""
    del production, doc  # Parentage is supplied by staff, never persisted here.
    saved: shutdown_record.SessionShutdown = {
        "login": "owner@example.com", "label": "claude2", "machine": "natedev",
        "requested_at": "2026-10-09T21:49:10+00:00", "request_id": "request",
        "requested_by": {"kind": "terminal"}, "hold": "session", "then": "stop", "force": "wait for ready",
        "alerts": "none", "probe": False,
        "session": {
            "session_id": "sid-alpha", "config_dir": str(root), "pid": 4242, "proc_start": "1",
            "name": name, "cwd": str(cwd or worktree), "status": "idle", "updated_at": 0, "probe": False,
            "model": {"kind": "no reply yet"},
            "checkout": {"kind": "git", "path": str(worktree), "head": {"kind": "branch", "name": branch},
                         "upstream": {"kind": "tracking", "ahead": 0}, "dirty": []} if branch else {"kind": "not a checkout"},
            "run_dirs": [], "codex_servers": [], "timers": [], "kind": "unit",
            "host": {"kind": "unit", "unit": unit,
                     "session_name": host_session, "session_host": "herdr",
                     "plan": {"kind": "plan", "path": str(plan)} if plan is not None else {"kind": "no run record"}},
        },
        "workers": [], "timers": [], "settle_message": {"kind": "not sent"}, "where": {"kind": "not said"},
        "progress": {"kind": "stopped", "at": "2026-10-09T21:50:00+00:00"},
        "stop_issues": [], "trail": [], "codex_seats": [],
    }
    if progress == "waiting":
        saved["progress"] = {"kind": "waiting"}
    elif progress == "ready":
        saved["progress"] = {"kind": "ready", "at": "2026-10-09T21:50:00+00:00"}
    return saved


def assert_shared_stop_record(case: unittest.TestCase, seed: Callable[[], Path], path: Path) -> None:
    """Required/default record fields come from one builder in every lifecycle fixture."""
    def with_new_default[**P](build: Callable[P, shutdown_record.SessionShutdown]) -> Callable[P, shutdown_record.SessionShutdown]:
        def changed(*args: P.args, **kwargs: P.kwargs) -> shutdown_record.SessionShutdown:
            saved = build(*args, **kwargs)
            saved["request_id"] = "shared-default-request"
            return saved
        return changed

    with mock.patch(__name__ + ".stopped_record", side_effect=with_new_default(stopped_record)) as builder:
        _ = seed()
    builder.assert_called_once()
    case.assertEqual(json.loads(path.read_text())["request_id"], "shared-default-request")


def record_shutdown(case: unittest.TestCase, pane: PaneShellFixture, path: Path, *,
                    destination: tuple[str, str, str]) -> None:
    """A shutdown boundary that records a clean stop and one marked relaunch."""
    import rename_unit
    import shutdown_record
    import unit_lookup
    import herdr
    stopped_event = threading.Event()
    initial = cast(dict[str, object], json.loads(path.read_text()))
    if cast(dict[str, object], initial['progress']).get('kind') == 'stopped':
        stopped_event.set()
    pane.environment['RENAME_UNIT_STOP_WAIT'] = str(TEST_HANG_GUARD_SECS)

    def shutdown(*arguments: str) -> None:
        pane.stop_calls.append(' '.join(arguments))
        if arguments[0] == 'stop':
            pane.stopped()
            fields = cast(dict[str, str], json.loads(path.read_text()))
            def stopped(saved: shutdown_record.SessionShutdown) -> None:
                saved['progress'] = {'kind':'stopped','at':'2026-10-09T21:50:00+00:00'}
                saved['stop_issues'] = []
            _ = shutdown_record.update_session(fields['login'], 'sid-alpha', stopped)
            stopped_event.set()
        elif arguments[0] == 'restart':
            pane.slug, pane.unit, name = destination
            pane.place = herdr.HerdrPlace('restored', 'w2:p1')
            write(pane.environment, [HostedUnit(pane.place.session, pane.place.pane,
                  {'SHOWRUNNER_UNIT':pane.slug, 'SHOWRUNNER_UNIT_ID':pane.unit})])
            pane.shell = 991001
            pane.found = unit_lookup.MarkedUnit(pane.unit,
                unit_lookup.LiveClaude(name, 'sid-alpha', '/fixture/socket', pane.shell), pane.place)
        else:
            raise AssertionError(f'unexpected shutdown: {arguments}')

    from scripts.shutdown import stop_resume
    def stop(identity: str) -> stop_resume.Stopped:
        shutdown("stop", identity)
        return stop_resume.Stopped(identity, pane.director)
    _ = case.enterContext(mock.patch.object(stop_resume, 'stop', side_effect=stop))
    _ = case.enterContext(mock.patch.object(rename_unit, 'run_shutdown', side_effect=shutdown))
    def wait_for_stop(_seconds: float) -> None:
        case.assertTrue(stopped_event.wait(TEST_HANG_GUARD_SECS), 'fake shutdown did not publish its stop')
    _ = case.enterContext(mock.patch.object(time, 'sleep', side_effect=wait_for_stop))
