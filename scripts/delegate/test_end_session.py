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

from scripts.delegate import remove_seats


END_SESSION = Path(__file__).with_name("end_session.sh")


class EndSessionTests(unittest.TestCase):
    """A roster remains terminal even when its app-server record is gone."""

    temporary: tempfile.TemporaryDirectory[str]  # pyright: ignore[reportUninitializedInstanceVariable]
    root: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    session_dir: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    marker: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    script: Path  # pyright: ignore[reportUninitializedInstanceVariable]
    calls: Path  # pyright: ignore[reportUninitializedInstanceVariable]
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

        scripts = self.root / "scripts"
        delegate = scripts / "delegate"
        agents = scripts / "agents"
        message = scripts / "message"
        delegate.mkdir(parents=True)
        agents.mkdir()
        message.mkdir()
        self.script = delegate / "end_session.sh"
        _ = shutil.copy2(END_SESSION, self.script)
        _ = (delegate / "remove_seats.py").write_text("", encoding="utf-8")
        _ = (agents / "codex_mesh.py").write_text("", encoding="utf-8")
        notifier = message / "notifier.sh"
        _ = notifier.write_text("#!/usr/bin/env zsh\nexit 0\n", encoding="utf-8")
        notifier.chmod(0o755)

        python_shim = self.root / ".claude" / "scripts" / "lib" / "py"
        python_shim.parent.mkdir(parents=True)
        _ = python_shim.write_text(
            "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"${END_SESSION_CALLS}\"\n",
            encoding="utf-8",
        )
        python_shim.chmod(0o755)
        self.session_id = session_id

    @override
    def tearDown(self) -> None:
        self.marker.unlink(missing_ok=True)
        self.temporary.cleanup()

    def test_roster_without_server_record_still_runs_mesh_stop(self) -> None:
        _ = (self.session_dir / "mesh_roster.json").write_text(json.dumps({
            "project-impl": {"thread_id": "thread-1", "status": "done"},
        }), encoding="utf-8")
        environment = os.environ.copy()
        environment.update({
            "HOME": str(self.root),
            "CLAUDE_CODE_SESSION_ID": self.session_id,
            "END_SESSION_CALLS": str(self.calls),
            "PLAN_DELEGATE_ACTIVE_DIR": str(self.marker.parent),
        })

        result = subprocess.run(
            ["bash", str(self.script)],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 3)
        self.assertIn("remove_seats.py --session-dir", calls[0])
        self.assertIn("codex_mesh.py stop --session-dir", calls[1])
        self.assertIn(str(self.session_dir), calls[1])
        self.assertIn("codex_mesh.py sweep --root", calls[2])
        self.assertIn(f"{self.root} --stop", calls[2])
        self.assertFalse(self.marker.exists())

    def test_phase_cleanup_sweep_is_best_effort(self) -> None:
        failed = subprocess.CompletedProcess[str](["codex_mesh.py"], 2, "", "")
        with patch.object(subprocess, "run", return_value=failed) as run, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            remove_seats.sweep_app_servers(self.root)

        command = cast("list[str]", run.call_args.args[0])
        self.assertEqual(
            command,
            [
                sys.executable,
                os.fspath(remove_seats.CODEX_MESH),
                "sweep",
                "--root",
                os.fspath(self.root),
                "--stop",
            ],
        )
        self.assertIn("seat cleanup continues", errors.getvalue())


if __name__ == "__main__":
    _ = unittest.main()
