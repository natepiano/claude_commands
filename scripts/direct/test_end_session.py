"""Regression tests for the unit run's terminal cleanup."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import override
from unittest.mock import patch

from scripts.copies import copies
from scripts.production.cli_fixture import ImmutableGitSeed
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.cargo_output_test_support import fake_cargo_output_roots
from scripts.production import staff
from scripts.production.staff_test_support import isolated_staff_file, fixture_member, fixture_worker, fixture_runtime_id


END_SESSION = Path(__file__).with_name("end_session.sh")
UNIT_RUNS = Path(__file__).resolve().parents[1] / "lib" / "unit_runs.py"
REMOVE_RUN_SCRATCH = Path(__file__).with_name("remove_run_scratch.py")


class EndSessionTests(unittest.TestCase):
    """Run cleanup keeps other directors' files."""

    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    marker: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    script: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    calls: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    runs: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session_id: str  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        _ = self.enterContext(fake_cargo_output_roots(children=True))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.session_dir = self.root / "run"
        self.session_dir.mkdir()
        session_id = fixture_runtime_id(f"end-session:{self.root.name}")
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", {}))
        owner = fixture_member("showrunner", name="end-owner")
        member = fixture_member("unit", name="end-unit", parent=owner.id, runtime_id=session_id)
        with (self.session_dir / "progress_history_state.json").open("w") as handle:
            json.dump({"owner_staff_id": member.id}, handle)
        active_root = self.root / "active"
        active_root.mkdir()
        self.marker = active_root / session_id
        _ = self.marker.write_text(f"{self.session_dir}\n", encoding="utf-8")
        self.calls = self.root / "calls"
        self.runs = self.root / "runs"
        self.runs.mkdir()

        scripts = self.root / "scripts"
        direct_dir = scripts / "direct"
        direct_dir.mkdir(parents=True)
        (scripts / "lib").mkdir()
        (scripts / "copies").mkdir()
        (scripts / "copies/copies.py").symlink_to(Path(copies.__file__))
        _ = shutil.copy2(UNIT_RUNS, scripts / "lib" / "unit_runs.py")
        self.script = direct_dir / "end_session.sh"
        _ = shutil.copy2(END_SESSION, self.script)
        # Import the real package, so the script's current imports resolve.
        (direct_dir / "remove_run_scratch.py").symlink_to(REMOVE_RUN_SCRATCH)
        _ = (direct_dir / "remove_workers.py").write_text("", encoding="utf-8")

        python_shim = scripts / "lib" / "py"
        _ = python_shim.write_text(
            # The run scan and scratch cleanup use private paths.
            "#!/usr/bin/env bash\ncase \"$1\" in */remove_run_scratch.py)"
            + " printf '%s\\n' \"$*\" >> \"${END_SESSION_CALLS}\";"
            + f" exec {sys.executable} \"$@\";; */unit_runs.py)"
            + f" exec {sys.executable} \"$@\";; */copies.py)"
            + f" if [[ ${{END_SESSION_REAL_COPIES:-}} == 1 ]]; then exec {sys.executable} \"$@\"; fi;; esac\n"
            + "printf '%s\\n' \"$*\" >> \"${END_SESSION_CALLS}\"\n",
            encoding="utf-8",
        )
        python_shim.chmod(0o755)
        self.session_id = session_id

    @override
    def tearDown(self) -> None:
        self.marker.unlink(missing_ok=True)

    def environment(self) -> dict[str, str]:
        environment = {name: value for name, value in os.environ.items()
                       if name != "CODEX_THREAD_ID" and not name.startswith("PLAN_DELEGATE_")}
        environment.update({
            "HOME": str(self.root),
            "PYTHONPATH": os.pathsep.join((str(END_SESSION.resolve().parents[2]), os.environ.get("PYTHONPATH", ""))),
            "CLAUDE_CODE_SESSION_ID": self.session_id,
            "CODEX_THREAD_ID": "",
            "COPIES_CONFIG": str(END_SESSION.resolve().parents[2] / "config/copies.conf"),
            "SCRATCH_COPIES_ROOT": str(self.root / "copies"),
            "END_SESSION_CALLS": str(self.calls),
            "UNIT_ACTIVE_DIR": str(self.marker.parent),
            "UNIT_RUN_ROOT": str(self.runs),
            "NOTIFIER_SESSIONS_DIR": str(self.root / "sessions"),
            "SHUTDOWN_STATE_DIR": str(self.root / "shutdown"),
            "SCRATCH_ROOT": str(self.root / "tracked-scratch"),
            "SCRATCH_VISIBLE_ROOT": str(self.root / "visible-scratch"),
            "SCRATCH_HOLDS": str(self.root / "scratch-holds.json"),
        })
        return environment

    def run_end(self, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.script)], capture_output=True, text=True,
                              env=environment, timeout=TEST_HANG_GUARD_SECS)

    def seed(self) -> Path:
        return ImmutableGitSeed.create(self.root / "repo", "main", "End Test",
                                       "end@example.invalid", {"Cargo.toml": "[workspace]\n", "file": "content"}).checkout

    def test_end_removes_only_the_units_idle_trees_and_workspaces(self) -> None:
        environment = self.environment()
        environment["END_SESSION_REAL_COPIES"] = "1"
        with patch.dict(os.environ, environment):
            source = self.seed()
            with copies.standing(str(next(row.id for row in staff.local_snapshot().rows if row.name == "end-unit")), source, copies.CommittedSource("HEAD")) as tree:
                pass
            with copies.standing("other-unit", source, copies.CommittedSource("HEAD")) as other:
                pass
            with copies.workspace(str(next(row.id for row in staff.local_snapshot().rows if row.name == "end-unit")), source, "HEAD", name="worker", branch="worker") as workspace:
                pass
            result = self.run_end(environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(tree.exists())
            self.assertFalse(workspace.exists())
            self.assertTrue(other.exists())
            self.assertFalse(self.marker.exists())

    def test_end_without_a_build_cache_has_no_standing_tree_to_remove(self) -> None:
        environment = self.environment()
        environment["END_SESSION_REAL_COPIES"] = "1"
        with patch.dict(os.environ, environment):
            owner = str(next(row.id for row in staff.local_snapshot().rows if row.name == "end-unit"))
            source = ImmutableGitSeed.create(self.root / "repo", "main", "End Test",
                                             "end@example.invalid", {"file": "content"}).checkout
            with copies.standing(owner, source, copies.CommittedSource("HEAD")) as tree:
                self.assertTrue(tree.is_relative_to(Path(environment["SCRATCH_ROOT"])))
                self.assertEqual(copies.owned_trees(owner).standing, ())
                self.assertTrue((tree / ".git").is_dir())
            self.assertFalse(tree.exists())
            result = self.run_end(environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(self.marker.exists())
            self.assertEqual(copies.owned_trees(owner).standing, ())

    def test_end_keeps_a_busy_tree_and_still_clears_the_marker(self) -> None:
        environment = self.environment()
        environment["END_SESSION_REAL_COPIES"] = "1"
        with patch.dict(os.environ, environment):
            source = self.seed()
            with copies.standing(str(next(row.id for row in staff.local_snapshot().rows if row.name == "end-unit")), source, copies.CommittedSource("HEAD")) as tree:
                result = self.run_end(environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("busy", result.stderr)
                self.assertTrue(tree.exists())
            self.assertFalse(self.marker.exists())


    def make_run(self, name: str, director: str) -> Path:
        run = self.runs / name
        (run / "verify_cache").mkdir(parents=True)
        (run / "phases" / "p3" / "gate" / ".git").mkdir(parents=True)
        _ = (run / "phases" / "p3" / "review.diff").write_text("diff\n", encoding="utf-8")
        (run / "p62_bin").mkdir()
        _ = (run / "p62_bin" / "CACHEDIR.TAG").write_text("tag\n", encoding="utf-8")
        membership = staff.local_snapshot()
        parent = membership.by_current_session(director)
        if parent is None:
            owner = next(row for row in membership.find(kind="showrunner") if row.name == "end-owner")
            parent = fixture_member("unit", name="other", parent=owner.id,
                                    runtime_id=fixture_runtime_id(director))
        _ = (run / "progress_history_state.json").write_text(
            json.dumps({"owner_staff_id": parent.id}), encoding="utf-8")
        return run

    def test_every_run_the_director_holds_is_cleaned_and_no_other(self) -> None:
        marked = self.make_run("run-marked", self.session_id)
        side = self.make_run("run-side", self.session_id)
        other = self.make_run("run-other", "another-director")
        _ = self.marker.write_text(f"{marked}\n", encoding="utf-8")

        result = self.run_end(self.environment())

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        unbinds = [line for line in calls if "astra/design_roles.py unbind-run" in line]
        self.assertEqual([line.split()[-1] for line in unbinds], [str(marked), str(side)])
        self.assertTrue(all(calls.index(line) < next(index for index, call in enumerate(calls)
                                                    if "remove_workers.py" in call) for line in unbinds))
        removal = next(line for line in calls if "remove_workers.py" in line)
        self.assertEqual(removal.split()[1:], ["--session-dir", str(marked), "--session-dir", str(side)])
        self.assertNotIn(str(other), "\n".join(calls))
        for run in (marked, side):
            self.assertFalse((run / "verify_cache").exists())
            self.assertFalse((run / "phases" / "p3" / "gate").exists())
            self.assertFalse((run / "p62_bin").exists())
            self.assertTrue((run / "phases" / "p3" / "review.diff").is_file())
        self.assertTrue((other / "verify_cache").is_dir())
        self.assertTrue((other / "phases" / "p3" / "gate" / ".git").is_dir())
        self.assertTrue((other / "p62_bin").is_dir())
        self.assertFalse(self.marker.exists())

    def test_held_runs_are_cleaned_after_the_marker_is_gone(self) -> None:
        side = self.make_run("run-side", self.session_id)
        other = self.make_run("run-other", "another-director")
        self.marker.unlink()

        result = self.run_end(self.environment())

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No active unit run marker", result.stdout)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        unbind = next(line for line in calls if "roles.py unbind-run" in line)
        removal = next(line for line in calls if "remove_workers.py" in line)
        self.assertLess(calls.index(unbind), calls.index(removal))
        self.assertEqual(unbind.split()[-1], str(side))
        self.assertEqual(removal.split()[1:], ["--session-dir", str(side)])
        self.assertNotIn(str(other), "\n".join(calls))
        self.assertFalse((side / "verify_cache").exists())
        self.assertTrue((other / "verify_cache").is_dir())

    def test_run_end_keeps_tracked_scratch_holding_a_paused_workers_live_command(self) -> None:
        scratch = self.root / "tracked-scratch" / "command-checkout"
        command_cwd = scratch / "build"
        command_cwd.mkdir(parents=True)
        _ = (scratch / ".scratch.json").write_text(json.dumps({"task": "run:run", "created": 1, "renewed": 1}))
        unused = scratch.parent / "unused-checkout"
        unused.mkdir()
        _ = (unused / ".scratch.json").write_text(json.dumps({"task": "run:run", "created": 1, "renewed": 1}))
        run = self.session_dir
        (run / "detached_runs").mkdir()
        record = run / "detached_runs" / "still-running.json"
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()",
                                  "detached_runs.py", "supervise", str(record)], stdin=subprocess.PIPE)
        self.addCleanup(lambda: child.communicate(timeout=TEST_HANG_GUARD_SECS))
        environment = self.environment()
        environment["CODEX_HOME"] = str(self.root / "codex")
        _ = self.enterContext(isolated_staff_file(self.root / "staff.json", environment))
        owner = fixture_member("showrunner", name="demo")
        parent = fixture_member("unit", name="builders", parent=owner.id,
                                worktree=staff.PresentWorktree("natedev", command_cwd))
        _ = (run / "progress_history_state.json").write_text(json.dumps({"owner_staff_id": parent.id}))
        member = fixture_worker("w7", parent.id, backend=staff.CodexBackend(Path(environment["CODEX_HOME"])))
        _ = record.write_text(json.dumps({
            "worker": "w7", "status": "running", "pid": child.pid, "staff_id": member.id}))
        environment.update({"SCRATCH_ROOT": str(scratch.parent), "SCRATCH_VISIBLE_ROOT": str(self.root / "visible-scratch"),
            "SCRATCH_HOLDS": str(self.root / "scratch-holds.json")})
        result = self.run_end(environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(command_cwd.is_dir(), result.stdout + result.stderr)
        self.assertFalse(unused.exists(), result.stdout + result.stderr)



if __name__ == "__main__":
    _ = unittest.main()
