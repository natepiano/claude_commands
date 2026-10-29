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

from scripts.direct import workers


END_SESSION = Path(__file__).with_name("end_session.sh")
UNIT_RUNS = Path(__file__).resolve().parents[1] / "lib" / "unit_runs.py"
REMOVE_RUN_SCRATCH = Path(__file__).with_name("remove_run_scratch.py")


class EndSessionTests(unittest.TestCase):
    """A roster remains terminal even when its app-server record is gone."""

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    marker: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    script: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    calls: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    runs: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session_id: str  # pyright: ignore[reportUninitializedInstanceVariable]

    @override
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.session_dir = self.root / "run"
        self.session_dir.mkdir()
        session_id = f"end-session-test-{os.getpid()}-{self.root.name}"
        active_root = self.root / "active"
        active_root.mkdir()
        self.marker = active_root / session_id
        _ = self.marker.write_text(f"{self.session_dir}\n", encoding="utf-8")
        self.calls = self.root / "calls"
        self.runs = self.root / "runs"
        self.runs.mkdir()

        scripts = self.root / "scripts"
        direct_dir = scripts / "direct"
        agents = scripts / "agents"
        direct_dir.mkdir(parents=True)
        agents.mkdir()
        (scripts / "lib").mkdir()
        _ = shutil.copy2(UNIT_RUNS, scripts / "lib" / "unit_runs.py")
        self.script = direct_dir / "end_session.sh"
        _ = shutil.copy2(END_SESSION, self.script)
        _ = shutil.copy2(REMOVE_RUN_SCRATCH, direct_dir / "remove_run_scratch.py")
        _ = (direct_dir / "remove_workers.py").write_text("", encoding="utf-8")
        (agents / "codex_mesh.py").symlink_to(END_SESSION.parents[1] / "agents" / "codex_mesh.py")

        python_shim = self.root / ".claude" / "scripts" / "lib" / "py"
        python_shim.parent.mkdir(parents=True)
        _ = python_shim.write_text(
            # The run scan and the scratch removal are real; every other script only records its call.
            "#!/usr/bin/env bash\ncase \"$1\" in */remove_run_scratch.py)"
            + " printf '%s\\n' \"$*\" >> \"${END_SESSION_CALLS}\";"
            + f" exec {sys.executable} \"$@\";; */unit_runs.py)"
            + f" exec {sys.executable} \"$@\";; esac\n"
            + "printf '%s\\n' \"$*\" >> \"${END_SESSION_CALLS}\"\n",
            encoding="utf-8",
        )
        python_shim.chmod(0o755)
        self.session_id = session_id

    @override
    def tearDown(self) -> None:
        self.marker.unlink(missing_ok=True)
        self.temporary.cleanup()

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update({
            "HOME": str(self.root),
            "CLAUDE_CODE_SESSION_ID": self.session_id,
            "END_SESSION_CALLS": str(self.calls),
            "UNIT_ACTIVE_DIR": str(self.marker.parent),
            "UNIT_RUN_ROOT": str(self.runs),
            "UNIT_WORKERS_STATE": str(self.root / "unit-workers"),
            "SCRATCH_ROOT": str(self.root / "tracked-scratch"),
            "SCRATCH_VISIBLE_ROOT": str(self.root / "visible-scratch"),
            "SCRATCH_HOLDS": str(self.root / "scratch-holds.json"),
        })
        return environment

    def test_roster_without_server_record_still_runs_mesh_stop(self) -> None:
        _ = (self.session_dir / "mesh_roster.json").write_text(json.dumps({
            "project-impl": {"thread_id": "thread-1", "status": "done"},
        }), encoding="utf-8")
        result = subprocess.run(
            ["bash", str(self.script)],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment(),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 4)
        self.assertTrue(calls[0].endswith(f"guilds/roles.py unbind-run {self.session_dir}"))
        self.assertIn("remove_workers.py --session-dir", calls[1])
        self.assertIn("codex_mesh.py stop", calls[2])
        self.assertIn("--pause", calls[2])
        self.assertIn("--session-dir", calls[2])
        self.assertIn(str(self.session_dir), calls[2])
        self.assertFalse(any("codex_mesh.py sweep" in call for call in calls))
        self.assertTrue(calls[3].endswith(f"remove_run_scratch.py --tracked {self.session_dir}"))
        self.assertFalse(self.marker.exists())

    def make_run(self, name: str, director: str) -> Path:
        run = self.runs / name
        (run / "verify_cache").mkdir(parents=True)
        (run / "phases" / "p3" / "gate" / ".git").mkdir(parents=True)
        _ = (run / "phases" / "p3" / "review.diff").write_text("diff\n", encoding="utf-8")
        (run / "p62_bin").mkdir()
        _ = (run / "p62_bin" / "CACHEDIR.TAG").write_text("tag\n", encoding="utf-8")
        _ = (run / "mesh_server.json").write_text("{}", encoding="utf-8")
        _ = (run / "director").write_text(f"{director}\n", encoding="utf-8")
        return run

    def test_every_run_the_director_holds_is_cleaned_and_no_other(self) -> None:
        marked = self.make_run("run-marked", self.session_id)
        side = self.make_run("run-side", self.session_id)
        other = self.make_run("run-other", "another-director")
        _ = self.marker.write_text(f"{marked}\n", encoding="utf-8")

        result = subprocess.run(
            ["bash", str(self.script)], check=False, capture_output=True, text=True, env=self.environment())

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        unbinds = [line for line in calls if "guilds/roles.py unbind-run" in line]
        self.assertEqual([line.split()[-1] for line in unbinds], [str(marked), str(side)])
        self.assertTrue(all(calls.index(line) < next(index for index, call in enumerate(calls)
                                                    if "remove_workers.py" in call) for line in unbinds))
        removal = next(line for line in calls if "remove_workers.py" in line)
        self.assertEqual(removal.split()[1:], ["--session-dir", str(marked), "--session-dir", str(side)])
        stops = [line.split()[line.split().index("--session-dir") + 1]
                 for line in calls if "codex_mesh.py stop" in line]
        self.assertTrue(all("--pause" in line.split() for line in calls if "codex_mesh.py stop" in line))
        self.assertEqual(stops, [str(marked), str(side)])
        sweeps = [line for line in calls if "codex_mesh.py sweep" in line]
        self.assertEqual(sweeps, [])
        self.assertNotIn(str(other), "\n".join(calls))
        for run in (marked, side):
            self.assertFalse((run / "verify_cache").exists())
            self.assertFalse((run / "phases" / "p3" / "gate").exists())
            self.assertFalse((run / "p62_bin").exists())
            self.assertTrue((run / "phases" / "p3" / "review.diff").is_file())
            self.assertTrue((run / "mesh_server.json").is_file())
        self.assertTrue((other / "verify_cache").is_dir())
        self.assertTrue((other / "phases" / "p3" / "gate" / ".git").is_dir())
        self.assertTrue((other / "p62_bin").is_dir())
        self.assertFalse(self.marker.exists())

    def test_held_runs_are_cleaned_after_the_marker_is_gone(self) -> None:
        side = self.make_run("run-side", self.session_id)
        other = self.make_run("run-other", "another-director")
        self.marker.unlink()

        result = subprocess.run(
            ["bash", str(self.script)], check=False, capture_output=True, text=True, env=self.environment())

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
        _ = (run / "progress_history_state.json").write_text(json.dumps({"production": "demo", "unit": "builders"}))
        (run / "mesh_runs").mkdir()
        record = run / "mesh_runs" / "still-running.json"
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()",
                                  "mesh_runs.py", "supervise", str(record)], stdin=subprocess.PIPE)
        self.addCleanup(lambda: child.communicate(timeout=4))
        _ = record.write_text(json.dumps({
            "worker": "w7", "status": "running", "pid": child.pid, "cwd": str(command_cwd)}))
        environment = self.environment()
        environment.update({"UNIT_WORKERS_STATE": str(self.root / "unit-workers"),
            "SCRATCH_ROOT": str(scratch.parent), "SCRATCH_VISIBLE_ROOT": str(self.root / "visible-scratch"),
            "SCRATCH_HOLDS": str(self.root / "scratch-holds.json")})
        with patch.dict(os.environ, environment):
            store = workers.UnitWorkerLedger(workers.ledger_path("demo", "builders"))
            with store.locked() as ledger:
                ledger.workers["w7"] = workers.UnitWorker("w7", "impl", "codex", "sol", "high", {},
                    workers.CodexThread("kept-thread", str(self.root / "codex")), workers.Paused(),
                    [workers.WorkerEvent("opened", "fixture", str(run), 1.0)])
        result = subprocess.run(["bash", str(self.script)], env=environment,
                                capture_output=True, text=True, check=False, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(command_cwd.is_dir(), result.stdout + result.stderr)
        self.assertFalse(unused.exists(), result.stdout + result.stderr)
        self.assertTrue(store.path.exists())



if __name__ == "__main__":
    _ = unittest.main()
