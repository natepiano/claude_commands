"""Find a unit's review history through the command with disposable local and Mac roots."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import final, override

from scripts.usage.collect import MAC_HOST
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


# Script completion deadlines only bound hangs; startup can be slow under load.
SCRIPT = Path(__file__).with_name("unit_run.py")
REPOSITORY = SCRIPT.resolve().parents[2]
EPOCH = datetime(2026, 10, 12, tzinfo=timezone.utc).timestamp()
UNIT = "review-pool-unit"
SSH_STUB = r'''
import json
import os
from pathlib import Path
import subprocess
import sys

with Path(os.environ["UNIT_RUN_SSH_LOG"]).open("a", encoding="utf-8") as log:
    _ = log.write(json.dumps(sys.argv[1:]) + "\n")
if os.environ.get("UNIT_RUN_SSH_FAIL") == "1":
    print("fake connection refused", file=sys.stderr)
    raise SystemExit(255)
environment = dict(os.environ)
environment["HOME"] = os.environ["UNIT_RUN_MAC_HOME"]
environment["UNIT_HISTORY_DIR"] = os.environ["UNIT_RUN_MAC_HISTORY"]
environment["XDG_CACHE_HOME"] = str(Path(environment["HOME"]) / ".cache")
result = subprocess.run(["/bin/bash", "-c", sys.argv[-1]], env=environment,
                        cwd=environment["HOME"], check=False)
raise SystemExit(result.returncode)
'''


@final
class UnitRunTests(unittest.TestCase):
    root: Path = Path()
    home: Path = Path()
    history: Path = Path()
    mac_history: Path = Path()
    production: Path = Path()
    worktree: Path = Path()
    ssh_log: Path = Path()
    environment: dict[str, str] = {}

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / "home"
        self.home.mkdir()
        self.history = self.root / "history"
        self.mac_history = self.root / "mac-history"
        self.production = self.root / "show-production.md"
        self.worktree = self.root / "worktrees" / "review pool"
        self.ssh_log = self.root / "ssh.jsonl"
        mac_home = self.root / "mac-home"
        mac_home.mkdir()
        (mac_home / ".claude").symlink_to(REPOSITORY, target_is_directory=True)
        bin_directory = self.root / "bin"
        bin_directory.mkdir()
        ssh = bin_directory / "ssh"
        _ = ssh.write_text(f"#!{sys.executable}\n{SSH_STUB}", encoding="utf-8")
        ssh.chmod(0o755)
        self.environment = {
            **os.environ,
            "HOME": str(self.home),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "PATH": str(bin_directory) + os.pathsep + os.environ.get("PATH", ""),
            "UNIT_HISTORY_DIR": str(self.history),
            "REVIEW_POOL_STATE": str(self.root / "review-pool"),
            "USAGE_STATE_DIR": str(self.root / "usage"),
            "UNIT_RUN_SSH_LOG": str(self.ssh_log),
            "UNIT_RUN_MAC_HOME": str(mac_home),
            "UNIT_RUN_MAC_HISTORY": str(self.mac_history),
            "UNIT_RUN_SSH_FAIL": "0",
            "CLAUDE_PYTHON": sys.executable,
        }
        self.write_production(str(self.worktree))

    def write_production(self, worktree: str) -> None:
        # Another table must not provide the unit's Worktree cell.
        _ = self.production.write_text("\n".join([
            "# Show", "", "## Notes", "| Unit | Worktree |", "| --- | --- |",
            f"| {UNIT} | /other/table |", "", "## Units", "",
            "| Unit | Plan | Worktree | Branch | Session | Port | Owns |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            f"| {UNIT} | `plan.md` | `{worktree}` | topic | director | — | scripts |",
            "", "## Dependencies", "",
        ]), encoding="utf-8")

    def events(self, history: Path, run_id: str, *events: dict[str, object]) -> Path:
        runs = history / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        path = runs / f"{run_id}.jsonl"
        _ = path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        return path

    def record(self, history: Path, run_id: str, worktree: str, seconds: float) -> Path:
        return self.events(history, run_id, {
            "event_type": "run_started", "working_dir": worktree,
            "run_started_at": EPOCH + seconds,
            "run_id": "event-id-is-not-the-session-name",
        })

    def run_script(self, *arguments: str, unit: str = UNIT) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(SCRIPT), "--production", str(self.production),
                               unit, *arguments], env=self.environment, capture_output=True,
                              text=True, check=False, timeout=TEST_HANG_GUARD_SECS)

    def assert_match(self, result: subprocess.CompletedProcess[str], run_id: str,
                     machine: str, seconds: float) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertEqual(len(result.stdout.splitlines()), 1)
        fields = result.stdout.rstrip("\n").split("\t")
        self.assertEqual(len(fields), 3)
        self.assertEqual(fields[:2], [run_id, machine])
        timestamp = datetime.fromisoformat(fields[2].replace("Z", "+00:00"))
        self.assertEqual(timestamp.utcoffset(), timedelta(0))
        self.assertEqual(timestamp, datetime.fromtimestamp(EPOCH + seconds, timezone.utc))

    def assert_one_mac_command(self) -> None:
        calls = self.ssh_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn(json.dumps(MAC_HOST), calls[0])

    def test_newest_start_wins_over_filename_and_file_modification_time(self) -> None:
        old = self.record(self.history, "zzz-old", str(self.worktree), 10)
        newest = self.record(self.history, "aaa-newest.session", str(self.worktree), 30.25)
        _ = self.record(self.history, "middle", str(self.worktree), 20)
        os.utime(old, (EPOCH + 100, EPOCH + 100))
        os.utime(newest, (EPOCH, EPOCH))
        self.assert_match(self.run_script("--root", str(self.history), "--no-mac"),
                          "aaa-newest.session", "natedev", 30.25)

    def test_a_newer_run_of_another_worktree_is_ignored(self) -> None:
        _ = self.record(self.history, "matching", str(self.worktree), 10)
        _ = self.record(self.history, "elsewhere", str(self.root / "elsewhere"), 100)
        self.assert_match(self.run_script("--no-mac"), "matching", "natedev", 10)

    def test_a_trailing_slash_in_the_worktree_cell_is_normalized(self) -> None:
        self.write_production(str(self.worktree) + "/")
        _ = self.record(self.history, "matching", str(self.worktree), 10)
        self.assert_match(self.run_script("--no-mac"), "matching", "natedev", 10)

    def test_run_working_dir_is_normalized_without_resolving_the_filesystem(self) -> None:
        _ = self.record(self.history, "matching", str(self.worktree) + "/missing/../", 10)
        self.assert_match(self.run_script("--no-mac"), "matching", "natedev", 10)

    def test_a_file_without_run_started_is_skipped(self) -> None:
        _ = self.events(self.history, "not-a-run", {
            "event_type": "phase_started", "working_dir": str(self.worktree),
            "run_started_at": EPOCH + 100,
        })
        _ = self.record(self.history, "matching", str(self.worktree), 10)
        self.assert_match(self.run_script("--no-mac"), "matching", "natedev", 10)

    def test_run_started_after_another_event_is_found(self) -> None:
        _ = self.events(self.history, "matching", {"event_type": "metadata"}, {
            "event_type": "run_started", "working_dir": str(self.worktree),
            "run_started_at": EPOCH + 10,
        }, {"event_type": "run_finished", "run_started_at": EPOCH + 100})
        self.assert_match(self.run_script("--no-mac"), "matching", "natedev", 10)

    def test_unreadable_run_files_are_skipped(self) -> None:
        _ = self.record(self.history, "matching", str(self.worktree), 10)
        (self.history / "runs" / "directory.jsonl").mkdir()
        _ = (self.history / "runs" / "invalid-text.jsonl").write_bytes(b"\xff\xfe")
        self.assert_match(self.run_script("--no-mac"), "matching", "natedev", 10)

    def test_no_units_row_reports_the_unit_and_does_not_call_ssh(self) -> None:
        result = self.run_script(unit="absent-unit")
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", "unit_run: no Units row for absent-unit\n"))
        self.assertFalse(self.ssh_log.exists())

    def test_no_run_with_no_mac_reports_the_unit_and_worktree(self) -> None:
        result = self.run_script("--no-mac")
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", f"unit_run: no run found for {UNIT} in {self.worktree}\n"))
        self.assertFalse(self.ssh_log.exists())

    def test_history_root_comes_from_the_environment_without_root_option(self) -> None:
        _ = self.record(self.history, "environment-run", str(self.worktree), 10)
        self.assert_match(self.run_script("--no-mac"), "environment-run", "natedev", 10)

    def test_explicit_root_takes_precedence_over_environment(self) -> None:
        _ = self.record(self.history, "environment-run", str(self.worktree), 100)
        explicit = self.root / "explicit-history"
        _ = self.record(explicit, "explicit-run", str(self.worktree), 10)
        self.assert_match(self.run_script("--root", str(explicit), "--no-mac"),
                          "explicit-run", "natedev", 10)

    def test_history_root_defaults_to_the_local_state_directory(self) -> None:
        _ = self.environment.pop("UNIT_HISTORY_DIR")
        _ = self.record(self.home / ".local/state/plan-delegate", "default-run", str(self.worktree), 10)
        self.assert_match(self.run_script("--no-mac"), "default-run", "natedev", 10)

    def test_a_local_match_returns_without_asking_the_mac(self) -> None:
        _ = self.record(self.history, "local-run", str(self.worktree), 10)
        _ = self.record(self.mac_history, "newer-mac-run", str(self.worktree), 100)
        self.assert_match(self.run_script(), "local-run", "natedev", 10)
        self.assertFalse(self.ssh_log.exists())

    def test_mac_fallback_selects_the_newest_matching_run_in_one_command(self) -> None:
        _ = self.record(self.history, "local-other", str(self.root / "elsewhere"), 100)
        _ = self.record(self.mac_history, "mac-old", str(self.worktree), 10)
        _ = self.record(self.mac_history, "mac-new", str(self.worktree) + "/", 20.5)
        _ = self.record(self.mac_history, "mac-other", str(self.root / "elsewhere"), 100)
        _ = self.events(self.mac_history, "not-a-run", {"event_type": "phase_started"})
        (self.mac_history / "runs" / "directory.jsonl").mkdir()
        _ = (self.mac_history / "runs" / "invalid-text.jsonl").write_bytes(b"\xff\xfe")
        self.assert_match(self.run_script(), "mac-new", "mac", 20.5)
        self.assert_one_mac_command()

    def test_no_match_on_either_machine_reports_no_run(self) -> None:
        result = self.run_script()
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", f"unit_run: no run found for {UNIT} in {self.worktree}\n"))
        self.assert_one_mac_command()

    def test_ssh_failure_reports_the_reason_and_counts_as_no_match(self) -> None:
        self.environment["UNIT_RUN_SSH_FAIL"] = "1"
        result = self.run_script()
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertEqual(result.stderr, "unit_run: Mac unavailable: fake connection refused\n"
                         + f"unit_run: no run found for {UNIT} in {self.worktree}\n")
        self.assert_one_mac_command()


if __name__ == "__main__":
    _ = unittest.main()
