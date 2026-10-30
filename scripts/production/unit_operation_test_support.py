"""Private pane-shell observations and refusal assertions shared by unit operations."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast, final
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_unit_host
import herdr
import rename_unit
import retire_unit
import transfer_unit
import unit_lookup
import unit_move
from rename_state import RenameRefused


@final
class PaneShellFixture:
    """Observe a director parented by a pane shell, or that shell after exit."""

    def __init__(self, case: unittest.TestCase, worktree: Path, process_root: Path, *,
                 live: bool, slug: str = "demo", unit: str = "alpha-unit") -> None:
        self.slug: str = slug
        self.unit: str = unit
        self.worktree: Path = worktree
        self.place: herdr.HerdrPlace = herdr.HerdrPlace("agents", "w1:p1")
        self.shell: int = 991551
        self.director: int = 4242
        self.environment: dict[str, str] = {"HERDR_PROC": str(process_root), "HERDR_PLATFORM": "linux"}
        self.inside: list[int] = [self.shell, self.director] if live else [self.shell]
        self.stop_calls: list[str] = []
        self.info: dict[str, object] = {"shell_pid": self.shell}
        self.found: unit_lookup.MarkedUnit | None = unit_lookup.MarkedUnit(unit,
            unit_lookup.LiveClaude("alpha", "sid-alpha", "/fixture/socket", self.director)
            if live else unit_lookup.ClaudeNotRunning(), self.place)
        marks = {unit_lookup.PRODUCTION_MARK: slug, unit_lookup.UNIT_MARK: unit}
        self.processes: list[herdr.HerdrProcess] = [herdr.HerdrProcess(
            self.shell, self.place, marks, "arbitrary-shell-name", str(worktree))]
        fake_unit_host.process(self.environment, self.shell, 991000, ["arbitrary-shell-name"], marks)
        self.parent(self.shell)
        _ = cast(object, case.enterContext(mock.patch.dict(os.environ, self.environment)))
        _ = case.enterContext(mock.patch.object(unit_move, "processes_inside", side_effect=self.inside_pids))
        _ = case.enterContext(mock.patch.object(unit_lookup, "marked_unit", side_effect=self.marked_unit))
        _ = case.enterContext(mock.patch.object(unit_lookup, "every_marked_unit", side_effect=self.marked))
        _ = case.enterContext(mock.patch.object(herdr, "herdr_processes", side_effect=self.observed_processes))
        _ = case.enterContext(mock.patch.object(herdr.Herdr, "result", side_effect=self.process_info))
        _ = case.enterContext(mock.patch.object(herdr.Herdr, "run", side_effect=AssertionError("no terminal action allowed")))
        _ = case.enterContext(mock.patch.object(rename_unit, "run_shutdown", side_effect=self.shutdown))
        _ = case.enterContext(mock.patch.object(rename_unit, "_stop_runs", side_effect=self.stop_runs))
        _ = case.enterContext(mock.patch.object(retire_unit, "stop_sessions", side_effect=self.stop_sessions))

    def parent(self, pid: int) -> None:
        fake_unit_host.process(self.environment, self.director, pid, ["claude"],
                               {unit_lookup.PRODUCTION_MARK: self.slug, unit_lookup.UNIT_MARK: self.unit})

    def inside_pids(self, _worktree: Path) -> list[int]:
        return list(self.inside)

    def marked_unit(self, slug: str, unit: str) -> unit_lookup.MarkedUnit | None:
        return self.found if (slug, unit) == (self.slug, self.unit) else None

    def marked(self) -> unit_lookup.MarkedProductions:
        return unit_lookup.MarkedProductions({self.slug: {self.unit: self.found}} if self.found else {}, {})

    def observed_processes(self) -> list[herdr.HerdrProcess]:
        return list(self.processes)

    def process_info(self, *arguments: str) -> dict[str, object]:
        if arguments != ("pane", "process-info", "--pane", self.place.pane):
            raise AssertionError(f"unexpected terminal request: {arguments}")
        return {"process_info": self.info}

    def shutdown(self, *arguments: str) -> None:
        self.stop_calls.append("shutdown " + " ".join(arguments))
        raise RenameRefused("fixture: preflight did not refuse before shutdown")

    def stop_runs(self, _target: rename_unit.StopTarget) -> None:
        self.stop_calls.append("stop runs")
        raise RenameRefused("fixture: preflight did not refuse before stopping runs")

    def stop_sessions(self, _unit: retire_unit.Unit, _found: retire_unit.Found,
                      _wait: float, _ids: frozenset[str]) -> list[str]:
        self.stop_calls.append("stop sessions")
        return ["fixture: preflight did not refuse before stopping sessions"]


def assert_pane_shell_refused(case: unittest.TestCase, pane: PaneShellFixture,
                             invoke: Callable[[], subprocess.CompletedProcess[str]], *,
                             journal: Path | None = None,
                             operation: Literal["rename", "transfer"] | None = None) -> None:
    """Repeat the real command: no stop boundary or journal progress is permitted."""
    before = journal.read_bytes() if journal is not None and journal.exists() else None
    expected = (f"{pane.unit}'s Claude runs in pane shell {pane.shell} "
                f"({unit_lookup.place_text(pane.place)}); a pane-shell unit cannot move yet (follow-up phase)")
    for _attempt in range(2):
        result = invoke()
        case.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        case.assertIn(expected, result.stdout + result.stderr)
        case.assertEqual(pane.stop_calls, [], "refusal must precede every shutdown and run stop")
        if journal is not None:
            after = journal.read_bytes() if journal.exists() else None
            case.assertEqual(after, before, "preflight refusal must not create or advance the journal")
            step = cast(dict[str, object], json.loads(after))["step"] if after is not None else 0
            case.assertEqual(step, 0)
        if _attempt == 0 and journal is not None and operation is not None:
            # Write a real step-zero journal by interrupting immediately before
            # its stop, with a normally exec-launched director during setup.
            # The second invocation must refuse the resumed request as well.
            original, shell = pane.found, pane.info["shell_pid"]
            pane.found = unit_lookup.MarkedUnit(pane.unit, unit_lookup.LiveClaude(
                "alpha", "sid-alpha", "/fixture/socket", pane.director), pane.place)
            pane.info["shell_pid"] = pane.director

            def pause(_target: rename_unit.StopTarget, _save: Callable[[], None]) -> None:
                raise RenameRefused("fixture: interrupted before stop")

            module = rename_unit if operation == "rename" else transfer_unit
            steps = rename_unit.STEPS if operation == "rename" else transfer_unit.STEPS
            try:
                with mock.patch.object(module, "STEPS", (steps[0]._replace(run=pause), *steps[1:])):
                    interrupted = invoke()
            finally:
                pane.found, pane.info["shell_pid"] = original, shell
            case.assertEqual(interrupted.returncode, 1, interrupted.stdout + interrupted.stderr)
            case.assertIn("fixture: interrupted before stop", interrupted.stderr)
            case.assertEqual(pane.stop_calls, [])
            before = journal.read_bytes()
            case.assertEqual(cast(dict[str, object], json.loads(before))["step"], 0)
