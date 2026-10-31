"""Seeded unit-status shell fixtures with static fake-host responses."""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Generator
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import ClassVar, cast, override
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / name)
                for name in ("production", "message", "herdr")]
from scripts.lib.socket_test_support import short_socket_directory
from scripts.production.staff_test_support import isolated_staff_file, showrunner_relationship, unit_membership
from dailies_input import StatusBlock, UnitRow, status_blocks
from unit_lookup import UnitState
import fake_unit_host
from fake_unit_host import HostedUnit
from sessions import SessionRecord
from fake_herdr import CommandResponse, pane_list

SCRIPT = Path(__file__).with_name("unit_status.sh")
SESSION_ID = "test-session"
# Only bound fixture hangs; no tested wait uses a clock.
# load 136 on natedev, 2026-10-29
TEST_HANG_GUARD_SECS = 300


def setUpModule() -> None:
    """Keep the tests' expected zone independent of the user's zone setting."""
    unittest.enterModuleContext(mock.patch.dict(os.environ, {"USER_ZONE": "America/Los_Angeles"}))


def _write_executable(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(contents, encoding="utf-8")
    path.chmod(0o755)


def publish_host(environment: dict[str, str], units: list[HostedUnit]) -> None:
    """Keep fake process ancestry, listener ownership, argv logs and exact response bytes.

    These tests use only static read responses, so generate their shell dispatch
    once per pane update instead of loading Python/JSON for every fake CLI call.
    Unknown commands fail loudly rather than silently accepting an untested call.
    """
    fake_unit_host.write(environment, units)
    responses = cast(dict[str, CommandResponse], json.loads(Path(environment["FAKE_HERDR_RESPONSES"]).read_text()))
    lines = ["#!/usr/bin/env zsh", 'case "$*" in']
    for command, response in responses.items():
        if command == "default":
            continue
        arguments = command.split()
        stdout = (pane_list(response["stdout"], arguments[:2], [])
                  if arguments[2:4] == ["pane", "list"] else response["stdout"])
        lines.extend((f"{shlex.quote(command)})",
                      f"print -r -- {shlex.quote(json.dumps(arguments))} >> \"$FAKE_HERDR_LOG\"",
                      f"print -rn -- {shlex.quote(stdout)}",
                      f"exit {response['code']};;"))
    lines.extend(('*) print -u2 -r -- "unexpected unit-status host command: $*"; exit 98;;', "esac"))
    _write_executable(Path(environment["HERDR_BIN"]), "\n".join(lines) + "\n")


class UnitStatusFixture(unittest.TestCase):
    fixture_parent: ClassVar[Path]
    fixture_seed: ClassVar[Path]
    fixture_case: ClassVar[Path]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.fixture_parent = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="unit-status-test-")))
        cls.fixture_seed = cls.fixture_parent / "seed"
        cls.fixture_case = cls.fixture_parent / "case"
        root = cls.fixture_case
        script = root / "scripts/production/unit_status.sh"
        script.parent.mkdir(parents=True)
        _ = shutil.copy2(SCRIPT, script)
        _write_executable(root / "scripts/lib/py", f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        message = root / "scripts/message"
        message.mkdir()
        _ = shutil.copy2(SCRIPT.parents[1] / "message/sessions.py", message / "sessions.py")
        _write_executable(message / "notifier.sh", 'print -r -- "$*" >> "$NOTIFIER_CALL_LOG"\nprint -r -- "$NOTIFIER_HEALTH_TEXT"\nexit "$NOTIFIER_HEALTH_EXIT"\n')
        hooks = root / "scripts/hooks"
        hooks.mkdir()
        _ = (hooks / "direct_run.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
        for name in ("production_units.py", "live_units.py", "showrunners.py", "unit_lookup.py", "unit_names.py", "staff.py", "session_pane.py"):
            _ = shutil.copy2(SCRIPT.with_name(name), script.with_name(name))
        (root / "scripts/herdr").mkdir()
        _ = shutil.copy2(SCRIPT.parents[1] / "herdr/herdr.py", root / "scripts/herdr/herdr.py")
        (root / "scripts/shutdown").mkdir()
        for name in ("pane_record.py", "remote.py"):
            _ = shutil.copy2(SCRIPT.parents[1] / "shutdown" / name, root / "scripts/shutdown" / name)
        for name in ("atomic_file.py", "user_zone.py", "probe_session.py", "unit_runs.py", "nix_env.py"):
            _ = shutil.copy2(SCRIPT.parents[1] / "lib" / name, root / "scripts/lib" / name)
        for name in ("active", "sessions"):
            (root / name).mkdir()
        _ = (root / "herdr-responses.json").write_text(json.dumps({"default": {"code": 0, "stdout": ""}}))
        _ = shutil.copytree(root, cls.fixture_seed)
        shutil.rmtree(root)

    @contextmanager
    def case_directory(self) -> Generator[Path]:
        _ = shutil.copytree(self.fixture_seed, self.fixture_case)
        try:
            yield self.fixture_case
        finally:
            shutil.rmtree(self.fixture_case)

    def live_pid(self) -> int:
        """A unique real live process whose lifetime ends on pipe EOF."""
        child = subprocess.Popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert child.stdin is not None
        self.addCleanup(child.wait, timeout=TEST_HANG_GUARD_SECS)
        self.addCleanup(child.stdin.close)
        return child.pid

    def run_statuses(
        self, marker: str | None, health_text: str, health_exit: int, *,
        panes: tuple[dict[str, str], ...],
        units: tuple[str, ...] = ("hook",),
        retired_units: tuple[str, ...] = (),
        stopped_units: tuple[str, ...] = (),
        missing_units: tuple[str, ...] = (),
        unreadable_units: tuple[str, ...] = (),
    ) -> tuple[tuple[str, ...], str | None, str]:
        with self.case_directory() as root, isolated_staff_file(root / "staff.json", environment := dict(os.environ)):
            script = root / "scripts/production/unit_status.sh"
            active_dir = root / "active"
            call_log = root / "notifier_calls"
            records = root / "sessions"
            responses_file = root / "herdr-responses.json"
            environment.update({
                "HOME": str(root),
                "UNIT_ACTIVE_DIR": str(active_dir),
                "UNIT_HISTORY_DIR": str(root / "history"),
                "NOTIFIER_STATE_DIR": str(root / "notifier"),
                "NOTIFIER_NOW_EPOCH": "20100",
                "NOTIFIER_CALL_LOG": str(call_log),
                "NOTIFIER_HEALTH_TEXT": health_text,
                "NOTIFIER_HEALTH_EXIT": str(health_exit),
                "NOTIFIER_SESSIONS_DIR": str(records),
                "HERDR_BIN": str(root / "fake-herdr"),
                "HERDR_PROC": str(root / "proc"),
                "HERDR_PLATFORM": "linux",
                "FAKE_HERDR_LOG": str(root / "herdr.jsonl"),
                "FAKE_HERDR_RESPONSES": str(responses_file),
            })
            _ = showrunner_relationship("example")
            for unit in units:
                if unit not in retired_units:
                    _ = unit_membership("example", unit)
            pids: dict[str, int] = {}
            _ = responses_file.write_text(json.dumps({"default": {"code": 0, "stdout": ""}}))
            rows: list[str] = []
            for unit in units:
                worktree = root / "worktrees" / unit
                worktree.mkdir(parents=True)
                _ = (worktree / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
                plan = "(retired by the user)" if unit in retired_units else "docs/plan.md"
                rows.append(f"| {unit} | {plan} | {worktree} | {unit} | — | — |")
                if unit in missing_units:
                    continue
                if unit not in stopped_units:
                    address = Path(self.enterContext(short_socket_directory())) / f"{unit}.sock"
                    held = socket.socket(socket.AF_UNIX)
                    held.bind(str(address))
                    self.addCleanup(held.close)
                    session_id = f"{SESSION_ID}-{unit}"
                    if marker is not None:
                        _ = (active_dir / session_id).write_text(marker, encoding="utf-8")
                    pids[unit] = self.live_pid()
                    record = SessionRecord(pid=pids[unit], sessionId=session_id, name=f"claude-{unit}",
                        messagingSocketPath=str(address), updatedAt=1,
                        tmux="", formerNames=[])
                    _ = (records / f"{unit}.json").write_text(
                        "{" if unit in unreadable_units else json.dumps(record))
            production_doc = root / "example-production.md"
            _ = production_doc.write_text("\n".join((
                "# Production", "## Units", "| Unit | Plan | Worktree | Branch | Port | Owns |",
                "| --- | --- | --- | --- | --- | --- |", *rows)), encoding="utf-8")
            zsh = shutil.which("zsh")
            if zsh is None:
                raise RuntimeError("zsh is required for unit status tests")
            outputs: list[str] = []
            for pane_by_unit in panes:
                hosted = [
                    HostedUnit(unit, f"w{index}:p1", {"SHOWRUNNER_UNIT": "example", "SHOWRUNNER_UNIT_ID": unit},
                               pane_by_unit.get(unit, "— holding: waiting on x\n"))
                    for index, unit in enumerate(units, start=1) if unit not in missing_units]
                publish_host(environment, hosted)
                for unit in hosted:
                    if unit.session in pids:
                        fake_unit_host.place_process(environment, pids[unit.session], unit.pane)
                result = subprocess.run(
                    [zsh, str(script), str(root / "status"), "America/Los_Angeles", "--production", str(production_doc)],
                    check=True, capture_output=True, text=True, env=environment, timeout=TEST_HANG_GUARD_SECS,
                )
                self.assertEqual(result.stderr, "")
                outputs.append(result.stdout)
            calls = call_log.read_text(encoding="utf-8") if call_log.exists() else None
            seen_path = root / "status" / "decisions_seen"
            seen = seen_path.read_text(encoding="utf-8") if seen_path.exists() else ""
            return tuple(outputs), calls, seen

    def run_status(
        self, marker: str | None, health_text: str, health_exit: int, *,
        stopped_units: tuple[str, ...] = (),
    ) -> tuple[str, str | None]:
        outputs, calls, _ = self.run_statuses(
            marker, health_text, health_exit,
            panes=({"hook": "— holding: waiting on x\n"},), stopped_units=stopped_units,
        )
        return outputs[0], calls

    def parse_status(self, output: str, units: tuple[str, ...] = ("hook",)) -> tuple[StatusBlock, ...]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "status.txt"
            _ = path.write_text(output, encoding="utf-8")
            return status_blocks(path, tuple(UnitRow(unit, unit, UnitState.RUNNING) for unit in units))
