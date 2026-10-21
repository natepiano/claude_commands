"""Regression tests for the delegate run's terminal cleanup."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast, override
from unittest.mock import patch

from scripts.direct import remove_workers


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
        delegate = scripts / "direct"
        agents = scripts / "agents"
        delegate.mkdir(parents=True)
        agents.mkdir()
        (scripts / "lib").mkdir()
        _ = shutil.copy2(UNIT_RUNS, scripts / "lib" / "unit_runs.py")
        self.script = delegate / "end_session.sh"
        _ = shutil.copy2(END_SESSION, self.script)
        _ = shutil.copy2(REMOVE_RUN_SCRATCH, delegate / "remove_run_scratch.py")
        _ = (delegate / "remove_workers.py").write_text("", encoding="utf-8")
        _ = (agents / "codex_mesh.py").write_text("", encoding="utf-8")

        python_shim = self.root / ".claude" / "scripts" / "lib" / "py"
        python_shim.parent.mkdir(parents=True)
        _ = python_shim.write_text(
            # The run scan and the scratch removal are real; every other script only records its call.
            "#!/usr/bin/env bash\ncase \"$1\" in */unit_runs.py|*/remove_run_scratch.py)"
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
            "PLAN_DELEGATE_ACTIVE_DIR": str(self.marker.parent),
            "PLAN_DELEGATE_RUN_ROOT": str(self.runs),
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
        self.assertEqual(len(calls), 3)
        self.assertIn("remove_workers.py --session-dir", calls[0])
        self.assertIn("codex_mesh.py stop --session-dir", calls[1])
        self.assertIn(str(self.session_dir), calls[1])
        self.assertIn("codex_mesh.py sweep --root", calls[2])
        self.assertIn(f"{self.root} --stop", calls[2])
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
        self.assertEqual(calls[0].split()[1:], ["--session-dir", str(marked), "--session-dir", str(side)])
        stops = [line.split()[-1] for line in calls if "codex_mesh.py stop" in line]
        self.assertEqual(stops, [str(marked), str(side)])
        sweeps = [line for line in calls if "codex_mesh.py sweep" in line]
        self.assertEqual(len(sweeps), 1)
        self.assertTrue(sweeps[0].endswith(f"codex_mesh.py sweep --root {self.runs} --stop"), sweeps)
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
        self.assertEqual(calls[0].split()[1:], ["--session-dir", str(side)])
        self.assertNotIn(str(other), "\n".join(calls))
        self.assertFalse((side / "verify_cache").exists())
        self.assertTrue((other / "verify_cache").is_dir())

    def test_phase_cleanup_sweep_is_best_effort(self) -> None:
        failed = subprocess.CompletedProcess[str](["codex_mesh.py"], 2, "", "")
        with patch.object(subprocess, "run", return_value=failed) as run, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            remove_workers.sweep_app_servers(self.root)

        command = cast("list[str]", run.call_args.args[0])
        self.assertEqual(
            command,
            [
                sys.executable,
                os.fspath(remove_workers.CODEX_MESH),
                "sweep",
                "--root",
                os.fspath(self.root),
                "--stop",
            ],
        )
        self.assertIn("worker cleanup continues", errors.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
