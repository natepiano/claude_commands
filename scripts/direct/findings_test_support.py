"""Exercise the findings CLI with private ledgers and fresh command configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import ModuleType
from typing import ClassVar, cast, override
import unittest
from unittest.mock import patch

from scripts.production.cli_fixture import invoke_cli
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

# Only the fsync test calls a function directly; never initialize it from live config.
with patch.dict(os.environ, {"UNIT_CONFIG": str(Path(__file__).resolve().parents[2] / "config/direct.conf")}):
    from scripts.direct import findings as findings

SCRIPT = Path(__file__).with_name("findings.py")
FINDINGS_CODE = compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec")



def invoke_findings(arguments: list[str], environment: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the real entry point, re-reading import-time limits on every command."""
    module = ModuleType("__main__")
    module.__file__ = str(SCRIPT)

    def main() -> int:
        exec(FINDINGS_CODE, vars(module))
        return 0

    return invoke_cli(module, main, arguments, environment, cwd)


# findings.py has no compiled limits, so every run needs a complete config.
# These are the machine's shipped values; a test that cares about one limit
# overrides just that key on top of them.
STANDARD_CONFIG = """\
MIN_REPAIR_BUDGET=3
REPAIR_ROUNDS_PER_FINDING=0.5
RUNAWAY_ROUNDS=5
MAX_FIX_ATTEMPTS=2
MAX_REOPENS=2
STALLED_ROUNDS=2
MAX_CONSECUTIVE_SAME_KIND_PASSES=3
MAX_REVIEW_CANCELLATIONS=1
ROUTING_FROM_ROUND=4
ASTRA_AFTER_FAILED_FIXES=2
"""


class FindingsLedgerFixture(unittest.TestCase):
    fixture_root: ClassVar[Path]
    fixture_snapshot: ClassVar[Path]

    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path = Path()
        self.session_dir: Path = Path()
        self.history_file: Path = Path()
        self.config_file: Path = Path()

    @classmethod
    @override
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(prefix="findings-test-")
        cls.addClassCleanup(directory.cleanup)
        cls.fixture_root = Path(directory.name) / "case"
        cls.fixture_snapshot = Path(directory.name) / "snapshot"
        (cls.fixture_root / "session").mkdir(parents=True)
        _ = (cls.fixture_root / "direct.conf").write_text(STANDARD_CONFIG, encoding="utf-8")
        _ = shutil.copytree(cls.fixture_root, cls.fixture_snapshot)
        shutil.rmtree(cls.fixture_root)

    @override
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {"UNIT_SYNC_WRITES": "0"}))
        _ = shutil.copytree(self.fixture_snapshot, self.fixture_root)
        self.addCleanup(shutil.rmtree, self.fixture_root)
        self.root = self.fixture_root
        self.session_dir = self.root / "session"
        self.history_file = self.root / "history" / "run.jsonl"
        self.config_file = self.root / "direct.conf"

    def invoke(self, arguments: tuple[str, ...], at: int) -> subprocess.CompletedProcess[str]:
        command = [*arguments, "--session-dir", str(self.session_dir)]
        # Retain executable and cross-process persistence/configuration coverage.
        if self._testMethodName in (
            "test_ids_are_stable_across_rounds",
            "test_an_unusable_config_value_stops_the_run",
        ):
            return subprocess.run(
                [sys.executable, str(SCRIPT), *command], capture_output=True, text=True,
                env=self.environment(at), check=False, timeout=TEST_HANG_GUARD_SECS,
            )
        return invoke_findings(command, self.environment(at), self.root)

    def run_command(self, *arguments: str, at: int = 1_000) -> str:
        result = self.invoke(self.dispatch_arguments(arguments), at)
        result.check_returncode()
        return result.stdout.strip()

    def run_failing_command(
        self, *arguments: str, at: int = 1_000, complete_dispatch: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if complete_dispatch:
            arguments = self.dispatch_arguments(arguments)
        return self.invoke(arguments, at)

    def environment(self, at: int) -> dict[str, str]:
        """Pin the clock and the config so a machine's own direct.conf cannot move a limit."""
        environment = os.environ.copy()
        environment["UNIT_NOW_EPOCH"] = str(at)
        environment["UNIT_CONFIG"] = str(self.config_file)
        # The gate reads pass events from the durable stream under this root.
        # Left unset it resolves to the machine's own history, where a phase id
        # this fixture invented would be answered by whatever happens to live
        # there.
        environment["UNIT_HISTORY_DIR"] = str(self.root / "history")
        return environment

    def write_config(self, text: str) -> None:
        """Write the standard limits with `text` appended; a repeated key there wins."""
        _ = self.config_file.write_text(STANDARD_CONFIG + text, encoding="utf-8")

    def write_partial_config(self, text: str) -> None:
        """Write `text` as the whole file, standard limits omitted."""
        _ = self.config_file.write_text(text, encoding="utf-8")

    @staticmethod
    def dispatch_arguments(arguments: tuple[str, ...]) -> tuple[str, ...]:
        # Older ledger cases exercise other rules with a valid accepted-check list.
        if arguments and arguments[0] == "dispatch" and "--accepted-checks" not in arguments:
            return (*arguments, "--accepted-checks", "none: fixture edits reach no accepted behavior")
        return arguments

    def write_progress_state(self, instance_id: str, phase_id: str = "phase-1") -> None:
        """Stand in for progress_history.py: the ledger reads phase identity from it."""
        state = {
            "history_file": str(self.history_file),
            "phase": {"instance_id": instance_id, "id": phase_id, "title": phase_id},
        }
        _ = (self.session_dir / "progress_history_state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )

    def write_pass_events(
        self,
        instance_id: str,
        passes: list[tuple[str, str, str]],
        slot_key: str = "team_slot",
    ) -> None:
        """Stand in for the recorder's durable stream: (kind, worker, status) rows.

        In the order the passes opened. An empty status leaves the pass open,
        which is the row a launcher still waiting on its agent contributes. An
        empty `slot_key` writes no worker field at all, which is every pass the
        back corpus recorded.
        """
        path = self.root / "history" / "runs" / f"{self.session_dir.name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        for index, (kind, slot, status) in enumerate(passes):
            started: dict[str, object] = {
                "event_type": "pass_started",
                "phase_instance_id": instance_id,
                "pass_instance_id": f"pass-{index}",
                "pass_kind": kind,
            }
            if slot_key:
                started[slot_key] = slot
            lines.append(json.dumps(started))
            if status:
                finished = {**started, "event_type": "pass_finished", "status": status}
                lines.append(json.dumps(finished))
        _ = path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def open_finding(self, severity: str, title: str, *, at: int = 1_000) -> str:
        return self.run_command(
            "open", "--severity", severity, "--title", title, "--caught-by", "worker", at=at
        )

    def gate(self, *, at: int = 1_000) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.run_command("gate", at=at)))

    def status(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.run_command("status")))

    def events(self) -> list[dict[str, object]]:
        if not self.history_file.exists():
            return []
        return [
            cast(dict[str, object], json.loads(line))
            for line in self.history_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def close_round(self, covers: list[str], *, verdict: str = "accepted") -> None:
        """Dispatch a batch, land it, and record every closure verdict for it.

        `landed` stands in for the launcher, which is what calls it in a real run
        once the repair worker exits cleanly.
        """
        _ = self.run_command("dispatch", "--covers", ",".join(covers))
        _ = self.run_command("landed")
        for finding_id in covers:
            _ = self.run_command("verdict", "--id", finding_id, "--state", verdict)

    def write_two_open_phases(self) -> None:
        """Phase 15 started first and stays open while Phase 16 is the newest."""
        state = {
            "history_file": str(self.history_file),
            "phase": {"instance_id": "phase-sixteen", "id": "16", "title": "16", "status": "active"},
            "open_phases": [{"instance_id": "phase-fifteen", "id": "15", "title": "15",
                             "status": "active"}],
        }
        _ = (self.session_dir / "progress_history_state.json").write_text(
            json.dumps(state), encoding="utf-8"
        )

    def phase_status(self, phase: str) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.run_command("status", "--phase", phase)))

    def record_astra(self, finding_id: str, *, unavailable: str = "") -> Path:
        note = self.root / f"{finding_id}-astra.md"
        _ = note.write_text("Finding: retry fails. Try 1 reused the stale handle; try 2 kept its owner.\n")
        decision = ("--unavailable", unavailable) if unavailable else ("--approach", "Replace the handle owner")
        _ = self.run_command("astra", "--id", finding_id, "--note", str(note), *decision)
        return note

    def write_repair_intervals(self, count: int, *, instance: str = "instance-a") -> None:
        """Unledgered fixes use distinct intervals even when fix_pass restarts at one."""
        path = self.root / "history" / "runs" / f"{self.session_dir.name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, object]] = []
        for index in range(count):
            start: dict[str, object] = {
                "event_type": "pass_started", "phase_instance_id": instance,
                "pass_instance_id": f"repair-{instance}-{index}", "pass_kind": "fix",
                "fix_pass": 1, "pass_started_at": 100 + index * 20,
                "timestamp_epoch": 100 + index * 20, "pass_activity": "repair failing tests",
            }
            records.extend([start, {**start, "event_type": "pass_finished",
                                    "timestamp_epoch": 110 + index * 20,
                                    "pass_elapsed_seconds": 10, "status": "completed"}])
        with path.open("a", encoding="utf-8") as stream:
            for record in records:
                _ = stream.write(json.dumps(record) + "\n")

    def route_finding(self, finding_id: str, *, reason: str = "spec-gap") -> str:
        return self.run_command(
            "route", "--id", finding_id, "--reason", reason, "--owner", "receiving-unit",
            "--work-order", "docs/follow-up.md — Phase 6 — Specify retry ownership",
            "--due", "Phase 6 checkpoint", "--evidence", "The Work Order has no owner rule",
        )
